from __future__ import annotations

import argparse
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from evidence_evolve.benchmark_bank import load_materialization_receipt
from evidence_evolve.hashing import sha256_file


def _safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts


def _verify_archive(path: Path, *, deep: bool) -> int:
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not names or not all(_safe_member(name) for name in names):
                raise ValueError(f"unsafe or empty zip archive: {path}")
            if deep and archive.testzip() is not None:
                raise ValueError(f"zip CRC failure: {path}")
            return len(names)
    if path.name.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz")):
        count = 0
        with tarfile.open(path, "r:*") as archive:
            for member in archive:
                if not _safe_member(member.name):
                    raise ValueError(f"unsafe tar member in {path}: {member.name}")
                count += 1
                if deep and member.isfile():
                    stream = archive.extractfile(member)
                    if stream is not None:
                        while stream.read(1024 * 1024):
                            pass
        if count == 0:
            raise ValueError(f"empty tar archive: {path}")
        return count
    with path.open("rb") as stream:
        if not stream.read(1):
            raise ValueError(f"empty materialized asset: {path}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--deep", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    receipt = load_materialization_receipt(args.receipt)
    results = []
    for asset in receipt.assets:
        path = (repo / asset.local_path).resolve()
        path.relative_to(repo)
        if path.stat().st_size != asset.bytes or sha256_file(path) != asset.sha256:
            raise ValueError(f"materialized asset binding mismatch: {asset.asset_id}")
        results.append(
            {
                "asset_id": asset.asset_id,
                "member_count": _verify_archive(path, deep=args.deep),
                "status": "PASS",
            }
        )
    print(json.dumps({"assets": results, "valid": True}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
