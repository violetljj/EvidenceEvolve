from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol

from evidence_evolve.artifacts import create_once_bytes
from evidence_evolve.hashing import sha256_bytes
from evidence_evolve.research_actions.models import (
    ActionExecutionResult,
    ActionOutcome,
    IntelligenceRecord,
    ResearchActionJob,
    SourceArtifact,
    SourceKind,
)


class ActionAuthorityRequired(PermissionError):
    pass


class IntelligenceTransport(Protocol):
    def get(self, url: str, *, headers: dict[str, str]) -> bytes: ...


class UrllibIntelligenceTransport:
    def __init__(self, timeout_seconds: int = 30):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def get(self, url: str, *, headers: dict[str, str]) -> bytes:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"intelligence source returned HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"intelligence source request failed: {exc.reason}") from exc


_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".json",
    ".kt",
    ".md",
    ".py",
    ".rs",
    ".ts",
    ".yaml",
    ".yml",
}


def _abstract_from_inverted_index(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in value.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        positioned.extend(
            (position, word) for position in positions if isinstance(position, int)
        )
    return " ".join(word for _, word in sorted(positioned))


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return normalized[:100] or "source"


def _compact_source_excerpt(value: str, *, limit: int = 1200) -> str:
    """Keep a bounded review hint while raw source remains in the snapshot."""

    lines = [line.strip() for line in value.splitlines() if line.strip()]
    excerpt = "\n".join(lines)
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 1].rstrip() + "…"


class LiteratureRepoIntelligenceExecutor:
    """Execute source-bound OpenAlex and GitHub research intelligence.

    OpenAlex search requires an API key. GitHub public repository inspection can
    run unauthenticated; a token is optional and is never persisted.
    """

    def __init__(
        self,
        *,
        run_dir: Path,
        openalex_api_key: str | None,
        github_token: str | None = None,
        transport: IntelligenceTransport | None = None,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.openalex_api_key = openalex_api_key
        self.github_token = github_token
        self.transport = transport or UrllibIntelligenceTransport()

    def execute(
        self, job: ResearchActionJob, action_dir: Path
    ) -> ActionExecutionResult:
        self.preflight(job)
        records: list[IntelligenceRecord] = []
        artifacts: list[SourceArtifact] = []
        issues: list[str] = []

        if job.max_papers:
            if not self.openalex_api_key:
                raise ActionAuthorityRequired(
                    "OpenAlex API key required for source-bound paper search"
                )
            try:
                paper_records, paper_artifacts = self._search_papers(job, action_dir)
                records.extend(paper_records)
                artifacts.extend(paper_artifacts)
            except Exception as exc:
                issues.append(f"OPENALEX_SEARCH_FAILED:{type(exc).__name__}:{exc}")

        if job.max_repositories:
            try:
                repo_records, repo_artifacts, repo_issues = self._inspect_repositories(
                    job, action_dir
                )
                records.extend(repo_records)
                artifacts.extend(repo_artifacts)
                issues.extend(repo_issues)
            except Exception as exc:
                issues.append(f"GITHUB_RESEARCH_FAILED:{type(exc).__name__}:{exc}")

        if records and issues:
            outcome = ActionOutcome.SUCCEEDED_WITH_GAPS
        elif records:
            outcome = ActionOutcome.SUCCEEDED
        elif issues:
            outcome = ActionOutcome.FAILED
        else:
            outcome = ActionOutcome.NOT_EVALUABLE
            issues.append("NO_INTELLIGENCE_RESULTS")
        return ActionExecutionResult(
            outcome=outcome,
            records=records,
            artifacts=artifacts,
            issues=issues,
        )

    def preflight(self, job: ResearchActionJob) -> None:
        if job.max_papers and not self.openalex_api_key:
            raise ActionAuthorityRequired(
                "OpenAlex API key required for source-bound paper search"
            )

    def _headers(self, *, github: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "EvidenceEvolve/0.1 research-intelligence",
        }
        if github:
            headers.update(
                {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
            )
            if self.github_token:
                headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    def _snapshot(
        self,
        *,
        action_dir: Path,
        name: str,
        body: bytes,
        source_url: str,
        media_type: str = "application/json",
    ) -> SourceArtifact:
        digest = sha256_bytes(body)
        artifact_id = f"SRC-{digest[:24]}"
        path = action_dir / "raw" / name
        if path.exists():
            if path.read_bytes() != body:
                raise RuntimeError(f"source snapshot drift: {path}")
        else:
            create_once_bytes(path, body)
        return SourceArtifact(
            artifact_id=artifact_id,
            path=path.relative_to(self.run_dir).as_posix(),
            sha256=digest,
            media_type=media_type,
            source_url=source_url,
        )

    def _search_papers(
        self, job: ResearchActionJob, action_dir: Path
    ) -> tuple[list[IntelligenceRecord], list[SourceArtifact]]:
        public_params = {
            "search": job.query,
            "filter": "is_retracted:false",
            "per_page": str(job.max_papers),
            "sort": "relevance_score:desc",
            "select": (
                "id,doi,title,display_name,publication_year,publication_date,type,"
                "cited_by_count,is_retracted,authorships,abstract_inverted_index,"
                "open_access,best_oa_location,primary_location,topics"
            ),
        }
        request_params = {**public_params, "api_key": self.openalex_api_key or ""}
        base = "https://api.openalex.org/works"
        request_url = f"{base}?{urllib.parse.urlencode(request_params)}"
        source_url = f"{base}?{urllib.parse.urlencode(public_params)}"
        body = self.transport.get(request_url, headers=self._headers())
        artifact = self._snapshot(
            action_dir=action_dir,
            name="openalex-search.json",
            body=body,
            source_url=source_url,
        )
        payload = json.loads(body)
        records: list[IntelligenceRecord] = []
        for work in payload.get("results", [])[: job.max_papers]:
            if not isinstance(work, dict):
                continue
            authors = []
            for authorship in work.get("authorships") or []:
                author = authorship.get("author") if isinstance(authorship, dict) else None
                if isinstance(author, dict) and author.get("display_name"):
                    authors.append(str(author["display_name"]))
            abstract = _abstract_from_inverted_index(work.get("abstract_inverted_index"))
            topics = [
                str(topic.get("display_name"))
                for topic in (work.get("topics") or [])[:5]
                if isinstance(topic, dict) and topic.get("display_name")
            ]
            location = work.get("best_oa_location") or work.get("primary_location") or {}
            landing = location.get("landing_page_url") if isinstance(location, dict) else None
            canonical = str(work.get("doi") or work.get("id") or landing or "")
            if not canonical:
                continue
            open_access = work.get("open_access") or {}
            records.append(
                IntelligenceRecord(
                    source_id=f"PAPER-{sha256_bytes(canonical.encode())[:20]}",
                    kind=SourceKind.PAPER,
                    canonical_id=canonical,
                    title=str(work.get("display_name") or work.get("title") or canonical),
                    url=str(landing or work.get("doi") or work.get("id")),
                    summary=(abstract or "Abstract unavailable in source metadata")[:8000],
                    authors=authors,
                    published_at=str(
                        work.get("publication_date") or work.get("publication_year") or ""
                    )
                    or None,
                    open_access=(
                        bool(open_access.get("is_oa"))
                        if isinstance(open_access, dict)
                        else None
                    ),
                    artifact_ids=[artifact.artifact_id],
                    applicability={
                        "source": "OpenAlex",
                        "work_type": str(work.get("type") or "unknown"),
                        "topics": ", ".join(topics),
                        "cited_by_count": str(work.get("cited_by_count") or 0),
                    },
                )
            )
        return records, [artifact]

    def _inspect_repositories(
        self, job: ResearchActionJob, action_dir: Path
    ) -> tuple[list[IntelligenceRecord], list[SourceArtifact], list[str]]:
        query = f"{job.query} in:name,description,readme archived:false"
        params = {
            "q": query,
            "per_page": str(job.max_repositories),
            "sort": "stars",
            "order": "desc",
        }
        url = f"https://api.github.com/search/repositories?{urllib.parse.urlencode(params)}"
        body = self.transport.get(url, headers=self._headers(github=True))
        search_artifact = self._snapshot(
            action_dir=action_dir,
            name="github-repository-search.json",
            body=body,
            source_url=url,
        )
        payload = json.loads(body)
        artifacts = [search_artifact]
        records: list[IntelligenceRecord] = []
        issues: list[str] = []
        for repository in payload.get("items", [])[: job.max_repositories]:
            if not isinstance(repository, dict) or not repository.get("full_name"):
                continue
            try:
                record, repo_artifacts = self._inspect_repository(
                    job, action_dir, repository
                )
                records.append(record)
                artifacts.extend(repo_artifacts)
            except Exception as exc:
                issues.append(
                    f"REPOSITORY_INSPECTION_FAILED:{repository.get('full_name')}:"
                    f"{type(exc).__name__}:{exc}"
                )
        return records, artifacts, issues

    def _inspect_repository(
        self,
        job: ResearchActionJob,
        action_dir: Path,
        repository: dict[str, object],
    ) -> tuple[IntelligenceRecord, list[SourceArtifact]]:
        full_name = str(repository["full_name"])
        default_branch = str(repository.get("default_branch") or "HEAD")
        prefix = _safe_name(full_name)
        headers = self._headers(github=True)

        commit_url = (
            f"https://api.github.com/repos/{full_name}/commits/"
            f"{urllib.parse.quote(default_branch, safe='')}"
        )
        commit_body = self.transport.get(commit_url, headers=headers)
        commit_artifact = self._snapshot(
            action_dir=action_dir,
            name=f"{prefix}-commit.json",
            body=commit_body,
            source_url=commit_url,
        )
        commit = json.loads(commit_body)
        commit_sha = str(commit["sha"])
        tree_sha = str(commit["commit"]["tree"]["sha"])

        tree_url = (
            f"https://api.github.com/repos/{full_name}/git/trees/{tree_sha}?recursive=1"
        )
        tree_body = self.transport.get(tree_url, headers=headers)
        tree_artifact = self._snapshot(
            action_dir=action_dir,
            name=f"{prefix}-tree.json",
            body=tree_body,
            source_url=tree_url,
        )
        tree = json.loads(tree_body)
        candidates = self._rank_paths(job.query, tree.get("tree") or [])
        selected = candidates[: job.max_source_files_per_repository]
        artifacts = [commit_artifact, tree_artifact]
        inspected_paths: list[str] = []
        snippets: list[str] = []
        for item in selected:
            blob_url = str(item["url"])
            blob_body = self.transport.get(blob_url, headers=headers)
            blob_artifact = self._snapshot(
                action_dir=action_dir,
                name=f"{prefix}-blob-{str(item['sha'])[:12]}.json",
                body=blob_body,
                source_url=blob_url,
            )
            artifacts.append(blob_artifact)
            blob = json.loads(blob_body)
            encoded = blob.get("content", "")
            if blob.get("encoding") == "base64" and isinstance(encoded, str):
                decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
                excerpt = _compact_source_excerpt(decoded)
                if excerpt:
                    snippets.append(f"[{item['path']}]\n{excerpt}")
            inspected_paths.append(str(item["path"]))

        license_value = repository.get("license")
        license_name = (
            str(license_value.get("spdx_id"))
            if isinstance(license_value, dict) and license_value.get("spdx_id")
            else None
        )
        description = str(repository.get("description") or "")
        summary = description
        if snippets:
            summary += "\nPinned source observations:\n" + "\n\n".join(snippets)
        record = IntelligenceRecord(
            source_id=f"REPO-{sha256_bytes((full_name + commit_sha).encode())[:20]}",
            kind=SourceKind.REPOSITORY,
            canonical_id=f"github:{full_name}@{commit_sha}",
            title=full_name,
            url=str(repository.get("html_url") or f"https://github.com/{full_name}"),
            summary=summary[:4000],
            license=license_name,
            repository_commit=commit_sha,
            inspected_paths=inspected_paths,
            artifact_ids=[artifact.artifact_id for artifact in artifacts],
            applicability={
                "source": "GitHub",
                "language": str(repository.get("language") or "unknown"),
                "default_branch": default_branch,
                "tree_truncated": str(bool(tree.get("truncated"))).lower(),
            },
        )
        return record, artifacts

    @staticmethod
    def _rank_paths(query: str, tree: list[object]) -> list[dict[str, object]]:
        tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z0-9_]{3,}", query)
        }
        ranked: list[tuple[int, str, dict[str, object]]] = []
        for raw in tree:
            if not isinstance(raw, dict) or raw.get("type") != "blob":
                continue
            path = str(raw.get("path") or "")
            size = raw.get("size")
            if not path or not raw.get("url") or not raw.get("sha"):
                continue
            if isinstance(size, int) and size > 200_000:
                continue
            if Path(path).suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            lower = path.lower()
            score = sum(4 for token in tokens if token in lower)
            score += 3 if lower.startswith(("src/", "lib/", "models/", "algorithms/")) else 0
            score += 2 if Path(path).name.lower().startswith(("readme", "model", "algorithm")) else 0
            score += 1 if Path(path).suffix.lower() in {".py", ".cpp", ".cc", ".rs"} else 0
            ranked.append((-score, path, raw))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in ranked]
