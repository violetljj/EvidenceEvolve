from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from evidence_evolve.hashing import sha256_bytes
from evidence_evolve.proposals.materializer import (
    ProposalMaterializationError,
    extract_search_replace_proposal,
    materialize_proposal,
)
from evidence_evolve.proposals.models import ProposalMaterializerMode


_PATCH_LOCK = threading.RLock()


def apply_evidence_diff_patch(
    patch_str: str,
    original_str: str | None = None,
    patch_dir: str | Path | None = None,
    original_path: str | Path | None = None,
    language: str = "python",
    verbose: bool = True,
) -> tuple[str, int, Path | None, str | None, str | None, Path | None]:
    """Shinka-compatible entrypoint backed by the EvidenceEvolve materializer."""
    del verbose
    if original_str is None and original_path is None:
        raise ValueError("Either original_str or original_path must be provided")
    original = (
        Path(str(original_path)).read_text(encoding="utf-8")
        if original_str is None
        else original_str
    )
    if language != "python":
        return original, 0, None, f"UNSUPPORTED_LANGUAGE: {language}", None, None
    target_bytes = original.encode("utf-8")
    target_sha256 = sha256_bytes(target_bytes)
    patch_sha256 = sha256_bytes(patch_str.encode("utf-8"))
    destination = Path(patch_dir) if patch_dir is not None else None
    patch_path = None
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
        patch_path = destination / "search_replace.txt"
        patch_path.write_text(patch_str, encoding="utf-8")
    try:
        proposal = extract_search_replace_proposal(
            proposal_id=f"shinka-{patch_sha256[:24]}",
            source_system="shinka",
            raw_response=patch_str,
            extracted_patch_text=patch_str,
            target_sha256=target_sha256,
        )
        candidate, receipt = materialize_proposal(proposal, target_bytes)
    except (ProposalMaterializationError, ValueError) as error:
        code = getattr(error, "code", "PROPOSAL_IR_INVALID")
        return original, 0, None, f"{code}: {error}", None, None

    output_path = None
    patch_text = None
    if destination is not None:
        from shinka.edit.apply_diff import write_git_diff

        original_path_out = destination / "original.py"
        output_path = destination / "main.py"
        original_path_out.write_text(original, encoding="utf-8")
        output_path.write_text(candidate, encoding="utf-8")
        diff_path = destination / "edit.diff"
        write_git_diff(
            original,
            candidate,
            filename=original_path_out.name,
            out_path=diff_path,
        )
        patch_text = diff_path.read_text(encoding="utf-8")
        (destination / "proposal_ir.json").write_text(
            json.dumps(proposal.model_dump(mode="json"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (destination / "materialization_receipt.json").write_text(
            json.dumps(receipt.model_dump(mode="json"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return (
        candidate,
        len(proposal.edits),
        output_path,
        None,
        patch_text,
        patch_path,
    )


@contextmanager
def installed_shinka_materializer(
    mode: ProposalMaterializerMode,
) -> Iterator[None]:
    """Install the shared adapter for one in-process Shinka runner invocation."""
    if mode == ProposalMaterializerMode.UPSTREAM_STRICT:
        yield
        return
    if mode != ProposalMaterializerMode.EVIDENCE_EVOLVE_V1:
        raise ValueError(f"unsupported proposal materializer: {mode}")
    import shinka.edit.async_apply as async_apply

    with _PATCH_LOCK:
        original = async_apply.apply_diff_patch
        async_apply.apply_diff_patch = apply_evidence_diff_patch
        try:
            yield
        finally:
            async_apply.apply_diff_patch = original


def run_official_shinka_with_materializer(upstream_args: list[str]) -> int:
    """Run the official CLI with only the shared execution adapter installed."""
    from shinka.cli import run as upstream_cli

    with installed_shinka_materializer(
        ProposalMaterializerMode.EVIDENCE_EVOLVE_V1
    ):
        return int(upstream_cli.main(upstream_args))
