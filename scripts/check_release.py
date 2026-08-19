#!/usr/bin/env python3
"""Fail closed when a wheel or sdist contains non-release material."""

from __future__ import annotations

import argparse
import glob
import hashlib
from pathlib import Path, PurePosixPath
import re
import tarfile
import zipfile


FORBIDDEN_MARKERS = (
    "Tree-" + "CSF",
    "tree-" + "chromatic",
    "Casas" + "-Alvero",
    "E:" + "\\math-ai-research",
    "projects" + "/",
    "tools" + ".autonomous_math_research",
    "." + "agents",
)
FORBIDDEN_TOKENS = (
    "R" + "23",
    "S" + "3",
    "S" + "4",
    "S" + "6",
    "S" + "7",
)
FORBIDDEN_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(re.escape(token) for token in FORBIDDEN_TOKENS)
    + r")(?![A-Za-z0-9])"
)
FORBIDDEN_PARTS = {"runs", "outcomes", "_runtime", "__pycache__"}
SECRET_FILE_NAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
WINDOWS_ABSOLUTE = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Za-z]:\\[^\\\r\n]+\\[^\\\r\n]+"
)
POSIX_MACHINE_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9])/(?:home|Users|private/var|tmp)/[^\s\"'`]+"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_archive(path: Path) -> list[tuple[str, bytes]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return [
                (info.filename, archive.read(info))
                for info in archive.infolist()
                if not info.is_dir()
            ]
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            result: list[tuple[str, bytes]] = []
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"cannot read archive member: {member.name}")
                result.append((member.name, stream.read()))
            return result
    raise ValueError(f"unsupported distribution: {path}")


def _relative_member(archive: Path, name: str) -> PurePosixPath:
    member = PurePosixPath(name.replace("\\", "/"))
    if archive.name.endswith(".tar.gz"):
        if len(member.parts) < 2:
            raise ValueError(f"sdist member lacks release root: {name}")
        member = PurePosixPath(*member.parts[1:])
    return member


def _check_member(archive: Path, name: str, payload: bytes) -> list[str]:
    issues: list[str] = []
    member = _relative_member(archive, name)
    lowered_parts = {part.lower() for part in member.parts}
    if member.is_absolute() or ".." in member.parts:
        issues.append("unsafe archive path")
    if lowered_parts & FORBIDDEN_PARTS:
        issues.append("generated runtime directory")
    if member.name.lower() in SECRET_FILE_NAMES or member.suffix.lower() in SECRET_SUFFIXES:
        issues.append("credential-like filename")
    normalized_name = member.as_posix()
    for marker in FORBIDDEN_MARKERS:
        if marker.lower() in normalized_name.lower():
            issues.append("project-specific archive path")
            break
    if FORBIDDEN_TOKEN_PATTERN.search(normalized_name):
        issues.append("project-specific archive path")

    text = payload.decode("utf-8", errors="ignore")
    for marker in FORBIDDEN_MARKERS:
        if marker in text:
            issues.append("project-specific or source-checkout content")
            break
    if FORBIDDEN_TOKEN_PATTERN.search(text):
        issues.append("project-specific or source-checkout content")
    if WINDOWS_ABSOLUTE.search(text) or POSIX_MACHINE_ABSOLUTE.search(text):
        issues.append("machine-absolute path")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        issues.append("credential-like content")
    return issues


def check_archive(path: Path) -> tuple[int, str]:
    members = _read_archive(path)
    failures: list[str] = []
    for name, payload in members:
        for issue in _check_member(path, name, payload):
            failures.append(f"{name}: {issue}")
    if failures:
        joined = "\n".join(failures)
        raise ValueError(f"release boundary failed for {path.name}:\n{joined}")
    return len(members), _sha256(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+")
    args = parser.parse_args()
    expanded: list[Path] = []
    for value in args.archives:
        matches = [Path(item) for item in glob.glob(value)]
        expanded.extend(matches or [Path(value)])
    for archive in expanded:
        count, digest = check_archive(archive.resolve())
        print(f"PASS {archive.name} files={count} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
