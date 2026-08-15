from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from evidence_evolve.proposals.models import (
    AppliedEdit,
    MatchMode,
    MaterializationReceipt,
    ProposalIR,
    SearchReplaceEdit,
)


PATCH_PATTERN = re.compile(
    r"^[ \t]*<{7}[ \t]+SEARCH[ \t]*\r?\n"
    r"(.*?)\r?\n^[ \t]*={7}[ \t]*\r?\n"
    r"(.*?)\r?\n^[ \t]*>{7}[ \t]+REPLACE[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
EVOLVE_START = re.compile(
    r"(?:#|//|!|<!--|\(\*)?[^\S\r\n]*EVOLVE-BLOCK-START"
    r"[^\S\r\n]*(?:-->|\*\))?"
)
EVOLVE_END = re.compile(
    r"(?:#|//|!|<!--|\(\*)?[^\S\r\n]*EVOLVE-BLOCK-END"
    r"[^\S\r\n]*(?:-->|\*\))?"
)
EVOLVE_MARKER_LINE = re.compile(
    r"^\s*(?:#|//|!|<!--|\(\*)?\s*EVOLVE-BLOCK-(?:START|END)"
    r"\s*(?:-->|\*\))?\s*$"
)


class ProposalMaterializationError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _LineRecord:
    comparable: str
    start: int
    end: int


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _extract_tag(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL)
    return match.group(1) if match else None


def _remove_evolve_marker_lines(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not EVOLVE_MARKER_LINE.match(line)
    )


def extract_search_replace_proposal(
    *,
    proposal_id: str,
    source_system: str,
    raw_response: str,
    target_sha256: str,
    extracted_patch_text: str | None = None,
    metadata: dict[str, object] | None = None,
) -> ProposalIR:
    patch_text = (
        raw_response if extracted_patch_text is None else extracted_patch_text
    )
    edits = []
    for match in PATCH_PATTERN.finditer(patch_text):
        search = _remove_evolve_marker_lines(match.group(1))
        replace = _remove_evolve_marker_lines(match.group(2))
        edits.append(SearchReplaceEdit(search=search, replace=replace))
    if not edits:
        raise ProposalMaterializationError(
            "PROPOSAL_EXTRACTION_FAILED",
            "response contains no SEARCH/REPLACE blocks",
        )
    return ProposalIR(
        proposal_id=proposal_id,
        source_system=source_system,
        target_sha256=target_sha256,
        raw_response_sha256=_sha256_text(raw_response),
        extracted_patch_sha256=_sha256_text(patch_text),
        name=_extract_tag(raw_response, "NAME"),
        description=_extract_tag(raw_response, "DESCRIPTION"),
        edits=edits,
        metadata=metadata or {},
    )


def _mutable_ranges(text: str) -> list[tuple[int, int]]:
    markers = [(match.end(), "start") for match in EVOLVE_START.finditer(text)]
    markers.extend((match.start(), "end") for match in EVOLVE_END.finditer(text))
    markers.sort(key=lambda item: item[0])
    stack: list[int] = []
    ranges = []
    for position, marker_type in markers:
        if marker_type == "start":
            stack.append(position)
        elif not stack:
            raise ProposalMaterializationError(
                "INVALID_TARGET_MARKERS", "EVOLVE-BLOCK-END has no matching start"
            )
        else:
            ranges.append((stack.pop(), position))
    if stack or not ranges:
        raise ProposalMaterializationError(
            "INVALID_TARGET_MARKERS", "target has unbalanced or missing EVOLVE markers"
        )
    return ranges


def _inside(span: tuple[int, int], ranges: list[tuple[int, int]]) -> bool:
    return any(span[0] >= start and span[1] <= end for start, end in ranges)


def _all_occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    occurrences = []
    position = 0
    while True:
        position = text.find(needle, position)
        if position < 0:
            return occurrences
        end = position + len(needle)
        starts_on_line = position == 0 or text[position - 1] in "\r\n"
        ends_on_line = end == len(text) or text[end] in "\r\n"
        if starts_on_line and ends_on_line:
            occurrences.append((position, end))
        position += max(1, len(needle))


def _line_records(text: str) -> list[_LineRecord]:
    records = []
    position = 0
    for raw_line in text.splitlines(keepends=True):
        body = raw_line.rstrip("\r\n")
        records.append(
            _LineRecord(
                comparable=body,
                start=position,
                end=position + len(body),
            )
        )
        position += len(raw_line)
    return records


def _unique_blank_insensitive_match(
    search: str, target: str
) -> tuple[int, int] | None:
    search_lines = [line for line in search.splitlines() if line.strip()]
    target_lines = [record for record in _line_records(target) if record.comparable.strip()]
    matches = []
    for index in range(len(target_lines) - len(search_lines) + 1):
        window = target_lines[index : index + len(search_lines)]
        if [record.comparable for record in window] == search_lines:
            matches.append((window[0].start, window[-1].end))
    if len(matches) > 1:
        raise ProposalMaterializationError(
            "AMBIGUOUS_SEARCH",
            "SEARCH matches multiple locations after blank-line normalization",
        )
    return matches[0] if matches else None


def _locate_edit(edit: SearchReplaceEdit, target: str) -> tuple[int, int, MatchMode]:
    exact = _all_occurrences(target, edit.search)
    if len(exact) > 1:
        raise ProposalMaterializationError(
            "AMBIGUOUS_SEARCH", "SEARCH matches multiple exact locations"
        )
    if exact:
        return exact[0][0], exact[0][1], MatchMode.EXACT_UNIQUE
    normalized = _unique_blank_insensitive_match(edit.search, target)
    if normalized is None:
        raise ProposalMaterializationError(
            "SEARCH_NOT_FOUND",
            "SEARCH is neither an exact unique match nor a unique blank-line-only match",
        )
    return normalized[0], normalized[1], MatchMode.IGNORE_BLANK_LINES_UNIQUE


def materialize_proposal(
    proposal: ProposalIR,
    target_bytes: bytes,
) -> tuple[str, MaterializationReceipt]:
    actual_target_sha256 = _sha256_bytes(target_bytes)
    if actual_target_sha256 != proposal.target_sha256:
        raise ProposalMaterializationError(
            "TARGET_HASH_MISMATCH",
            f"expected={proposal.target_sha256} actual={actual_target_sha256}",
        )
    try:
        current = target_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProposalMaterializationError(
            "TARGET_ENCODING_INVALID", "target must be UTF-8"
        ) from error
    original_marker_count = (
        len(EVOLVE_START.findall(current)),
        len(EVOLVE_END.findall(current)),
    )
    _mutable_ranges(current)
    applied = []
    for index, edit in enumerate(proposal.edits):
        if "EVOLVE-BLOCK-" in edit.replace:
            raise ProposalMaterializationError(
                "MARKER_INJECTION_REJECTED", "replacement cannot contain EVOLVE markers"
            )
        start, end, mode = _locate_edit(edit, current)
        if not _inside((start, end), _mutable_ranges(current)):
            raise ProposalMaterializationError(
                "IMMUTABLE_EDIT_REJECTED", "matched SEARCH is outside editable regions"
            )
        current = current[:start] + edit.replace + current[end:]
        applied.append(
            AppliedEdit(
                edit_index=index,
                match_mode=mode,
                target_start=start,
                target_end=end,
            )
        )
    final_marker_count = (
        len(EVOLVE_START.findall(current)),
        len(EVOLVE_END.findall(current)),
    )
    _mutable_ranges(current)
    if final_marker_count != original_marker_count:
        raise ProposalMaterializationError(
            "MARKERS_CHANGED", "candidate changed the EVOLVE marker structure"
        )
    receipt = MaterializationReceipt(
        proposal_id=proposal.proposal_id,
        target_sha256=actual_target_sha256,
        candidate_sha256=_sha256_text(current),
        applied_edits=applied,
    )
    return current, receipt
