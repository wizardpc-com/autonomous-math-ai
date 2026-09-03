from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Iterable
import zipfile

from ..models import CandidateEvent
from . import atomic_write_json, file_digest


PORTABLE_SCHEMES = ("project://", "campaign://", "epoch://")
_SENSITIVE_FILE_NAMES = {
    ".env", "auth.json", "credentials.json", "secrets.json",
    "id_rsa", "id_ed25519",
}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


def _sensitive_archive_member(parts: tuple[str, ...]) -> bool:
    folded = tuple(part.casefold() for part in parts)
    name = folded[-1] if folded else ""
    suffix = PurePosixPath(name).suffix.casefold()
    return bool(
        ".git" in folded
        or name in _SENSITIVE_FILE_NAMES
        or name.startswith(".env.")
        or suffix in _SENSITIVE_SUFFIXES
    )


def _zip_members(path: Path) -> list[dict[str, object]] | None:
    if not zipfile.is_zipfile(path):
        if path.suffix.casefold() == ".zip":
            raise ValueError(f"required ZIP is invalid: {path.name}")
        return None
    members: list[dict[str, object]] = []
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                raw_name = info.orig_filename
                if not raw_name or "\x00" in raw_name or "\\" in raw_name:
                    raise ValueError("ZIP member name is empty, ambiguous, or contains NUL")
                member = PurePosixPath(raw_name)
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or any(part in {"", "."} for part in member.parts)
                    or (member.parts and member.parts[0].endswith(":"))
                ):
                    raise ValueError(f"ZIP member path escapes its archive: {raw_name}")
                if raw_name in seen:
                    raise ValueError(f"ZIP contains a duplicate member: {raw_name}")
                seen.add(raw_name)
                if _sensitive_archive_member(member.parts):
                    raise ValueError(
                        f"ZIP member is blocked as potentially credential-bearing: {raw_name}"
                    )
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) == stat.S_IFLNK:
                    raise ValueError(f"ZIP symbolic-link member is forbidden: {raw_name}")
                if info.flag_bits & 0x1:
                    raise ValueError(f"encrypted ZIP member is forbidden: {raw_name}")
                digest: str | None = None
                if not info.is_dir():
                    hasher = sha256()
                    with archive.open(info, "r") as source:
                        while chunk := source.read(1024 * 1024):
                            hasher.update(chunk)
                    digest = hasher.hexdigest()
                members.append({
                    "name": raw_name,
                    "is_dir": info.is_dir(),
                    "size": info.file_size,
                    "compressed_size": info.compress_size,
                    "crc32": f"{info.CRC:08x}",
                    "compression": info.compress_type,
                    "sha256": digest,
                })
    except zipfile.BadZipFile as exc:
        raise ValueError(f"required ZIP is invalid: {path.name}") from exc
    return members


def portable_project_uri(project_root: Path, path: Path) -> str:
    root = project_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("artifact is outside the target project")
    return "project://" + resolved.relative_to(root).as_posix()


def resolve_portable_uri(
    project_root: Path,
    runtime_root: Path,
    value: str,
) -> Path:
    """Resolve a durable URI without depending on the harness source layout."""
    scheme, tail = _safe_uri_tail(value)
    project = project_root.resolve()
    runtime = runtime_root.resolve()
    if scheme == "project://":
        root = project
        target = (root / Path(*tail.parts)).resolve()
    elif scheme == "epoch://":
        if len(tail.parts) < 2:
            raise ValueError(f"invalid durable artifact URI: {value}")
        root = (runtime / "runs" / tail.parts[0]).resolve()
        target = (root / Path(*tail.parts[1:])).resolve()
    else:
        if len(tail.parts) < 2:
            raise ValueError(f"invalid durable artifact URI: {value}")
        root = (runtime / "campaigns" / tail.parts[0]).resolve()
        target = (root / Path(*tail.parts[1:])).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"durable artifact URI is unavailable: {value}")
    return target


def _safe_uri_tail(value: str) -> tuple[str, PurePosixPath]:
    scheme = next((item for item in PORTABLE_SCHEMES if value.startswith(item)), None)
    if scheme is None:
        raise ValueError(f"unsupported durable artifact URI: {value}")
    tail = PurePosixPath(value[len(scheme):])
    if tail.is_absolute() or ".." in tail.parts or not tail.parts:
        raise ValueError(f"invalid durable artifact URI: {value}")
    return scheme, tail


@dataclass(slots=True)
class ArtifactStore:
    project_root: Path
    campaign_id: str
    epoch_id: str
    epoch_root: Path

    @property
    def bundle_root(self) -> Path:
        return self.epoch_root / "candidate_bundles"

    def resolve_uri(self, value: str) -> Path:
        return resolve_portable_uri(
            self.project_root,
            self.epoch_root.parent.parent,
            value,
        )

    def _source_path(self, value: str) -> tuple[Path, str]:
        if value.startswith(PORTABLE_SCHEMES):
            path = self.resolve_uri(value)
            return path, value
        raw = Path(value)
        path = (
            (self.project_root / raw).resolve()
            if not raw.is_absolute() else raw.resolve()
        )
        if not path.is_relative_to(self.project_root.resolve()) or not path.is_file():
            raise ValueError(f"candidate artifact is unavailable or outside project: {value}")
        if path.is_symlink():
            raise ValueError(f"candidate artifact cannot be a symbolic link: {value}")
        return path, portable_project_uri(self.project_root, path)

    def seal_producer_evidence_closure(
        self,
        *,
        producer_task_id: str,
        producer_job_id: str,
        task_packet: Path,
        required_file_access: list[dict[str, str]],
    ) -> dict[str, object]:
        closure_root = (
            self.epoch_root / "producer_evidence_closure" / producer_job_id
        ).resolve()
        if not closure_root.is_relative_to(self.epoch_root.resolve()):
            raise ValueError("producer evidence closure escapes the epoch")

        def seal_file(source: Path, relative: Path) -> dict[str, object]:
            resolved = source.resolve()
            if (
                not resolved.is_relative_to(self.epoch_root.resolve())
                or not resolved.is_file()
                or resolved.is_symlink()
            ):
                raise ValueError("producer evidence input is unavailable outside the epoch")
            if _sensitive_archive_member((resolved.name,)):
                raise ValueError(
                    "producer evidence input is blocked as potentially credential-bearing"
                )
            digest = file_digest(resolved)
            target = (closure_root / relative / digest / resolved.name).resolve()
            if not target.is_relative_to(closure_root):
                raise ValueError("producer evidence target escapes its closure")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file() or file_digest(target) != digest:
                    raise ValueError("producer evidence closure collision")
            else:
                shutil.copy2(resolved, target)
            uri = (
                f"epoch://{self.epoch_id}/"
                f"{target.relative_to(self.epoch_root.resolve()).as_posix()}"
            )
            return {
                "uri": uri,
                "sha256": digest,
                "size": target.stat().st_size,
                "zip_members": _zip_members(target),
            }

        packet_entry = seal_file(task_packet, Path("task_packet"))
        input_entries: list[dict[str, object]] = []
        for index, item in enumerate(required_file_access):
            if set(item) != {"reference", "path", "sha256"}:
                raise ValueError("materialized required_file_access entry is invalid")
            sealed = seal_file(Path(item["path"]), Path("required") / f"{index:03d}")
            if sealed["sha256"] != item["sha256"]:
                raise ValueError("required_file_access digest changed before closure sealing")
            input_entries.append({"reference": item["reference"], **sealed})
        manifest_path = closure_root / "MANIFEST.json"
        atomic_write_json(manifest_path, {
            "schema_version": 1,
            "producer_task_id": producer_task_id,
            "producer_job_id": producer_job_id,
            "task_packet": packet_entry,
            "required_file_access": input_entries,
        })
        manifest_uri = (
            f"epoch://{self.epoch_id}/"
            f"{manifest_path.relative_to(self.epoch_root.resolve()).as_posix()}"
        )
        return {
            "schema_version": 1,
            "producer_task_id": producer_task_id,
            "producer_job_id": producer_job_id,
            "manifest_uri": manifest_uri,
            "manifest_sha256": file_digest(manifest_path),
        }

    def verify_producer_evidence_closure(
        self, binding: dict[str, object],
    ) -> tuple[bool, dict[str, object]]:
        observed: dict[str, object] = {}
        try:
            expected_binding_keys = {
                "schema_version", "producer_task_id", "producer_job_id",
                "manifest_uri", "manifest_sha256",
            }
            if set(binding) != expected_binding_keys or binding["schema_version"] != 1:
                raise ValueError("producer evidence closure binding is invalid")
            manifest_uri = str(binding["manifest_uri"])
            manifest_path = self.resolve_uri(manifest_uri)
            manifest_digest = file_digest(manifest_path)
            observed["manifest_sha256"] = manifest_digest
            if manifest_digest != binding["manifest_sha256"]:
                raise ValueError("producer evidence closure manifest changed")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if set(manifest) != {
                "schema_version", "producer_task_id", "producer_job_id",
                "task_packet", "required_file_access",
            } or manifest["schema_version"] != 1:
                raise ValueError("producer evidence closure manifest is invalid")
            if (
                manifest["producer_task_id"] != binding["producer_task_id"]
                or manifest["producer_job_id"] != binding["producer_job_id"]
            ):
                raise ValueError("producer evidence closure identity changed")
            prefix = manifest_uri.rsplit("/", 1)[0] + "/"

            def verify_entry(entry: object, *, has_reference: bool) -> None:
                expected = {"uri", "sha256", "size", "zip_members"}
                if has_reference:
                    expected.add("reference")
                if not isinstance(entry, dict) or set(entry) != expected:
                    raise ValueError("producer evidence closure entry is invalid")
                uri = str(entry["uri"])
                if not uri.startswith(prefix):
                    raise ValueError("producer evidence closure entry escapes its directory")
                path = self.resolve_uri(uri)
                if path.is_symlink():
                    raise ValueError("producer evidence closure entry is a symbolic link")
                digest = file_digest(path)
                observed[uri] = digest
                if digest != entry["sha256"] or path.stat().st_size != entry["size"]:
                    raise ValueError("producer evidence closure entry changed")
                if _zip_members(path) != entry["zip_members"]:
                    raise ValueError("producer evidence ZIP member manifest changed")

            verify_entry(manifest["task_packet"], has_reference=False)
            entries = manifest["required_file_access"]
            if not isinstance(entries, list):
                raise ValueError("producer required_file_access manifest is invalid")
            for entry in entries:
                verify_entry(entry, has_reference=True)
            return True, observed
        except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            observed["error"] = str(exc)
            return False, observed

    def materialize_producer_evidence_closure(
        self, binding: dict[str, object], target_root: Path,
    ) -> dict[str, object]:
        valid, observed = self.verify_producer_evidence_closure(binding)
        if not valid:
            raise ValueError(f"producer evidence closure is unavailable or changed: {observed}")
        manifest_source = self.resolve_uri(str(binding["manifest_uri"]))
        manifest = json.loads(manifest_source.read_text(encoding="utf-8"))
        target = (target_root.resolve() / "producer_evidence_closure").resolve()
        if not target.is_relative_to(target_root.resolve()):
            raise ValueError("auditor producer evidence target escapes its workspace")

        def copy_entry(entry: dict[str, object], category: str) -> Path:
            source = self.resolve_uri(str(entry["uri"]))
            destination = target / category / str(entry["sha256"]) / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if not destination.is_file() or file_digest(destination) != entry["sha256"]:
                    raise ValueError("auditor producer evidence copy has an invalid digest")
            else:
                shutil.copy2(source, destination)
            return destination

        task_packet = copy_entry(manifest["task_packet"], "task_packet")
        required = []
        for entry in manifest["required_file_access"]:
            required.append({
                "reference": entry["reference"],
                "path": str(copy_entry(entry, "required")),
                "sha256": entry["sha256"],
                "size": entry["size"],
                "zip_members": entry["zip_members"],
            })
        manifest_target = target / "MANIFEST.json"
        shutil.copy2(manifest_source, manifest_target)
        return {
            "binding": dict(binding),
            "manifest": str(manifest_target),
            "task_packet": str(task_packet),
            "required_file_access": required,
        }

    def seal_candidate(
        self,
        event: CandidateEvent,
        *,
        producer_evidence_closure: dict[str, object] | None = None,
    ) -> dict[str, str]:
        entries: list[dict[str, object]] = []
        sealed_paths: list[str] = []
        hashes: dict[str, str] = {}
        for raw in event.artifact_paths:
            source, source_ref = self._source_path(raw)
            digest = file_digest(source)
            safe_name = source.name or "artifact"
            relative = Path("candidate_bundles") / event.fingerprint / digest / safe_name
            target = (self.epoch_root / relative).resolve()
            if not target.is_relative_to(self.epoch_root.resolve()):
                raise ValueError("candidate bundle target escapes epoch")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file() or file_digest(target) != digest:
                    raise ValueError("content-addressed candidate bundle collision")
            else:
                shutil.copy2(source, target)
            uri = f"epoch://{self.epoch_id}/{relative.as_posix()}"
            sealed_paths.append(uri)
            hashes[uri] = digest
            entries.append({
                "uri": uri,
                "sha256": digest,
                "size": target.stat().st_size,
                "source_ref": source_ref,
            })
        event.artifact_paths = sealed_paths
        manifest = self.bundle_root / event.fingerprint / "MANIFEST.json"
        atomic_write_json(manifest, {
            "schema_version": 2 if producer_evidence_closure is not None else 1,
            "candidate_fingerprint": event.fingerprint,
            "representation_id": event.representation_id,
            "artifacts": entries,
            **(
                {"producer_evidence_closure": producer_evidence_closure}
                if producer_evidence_closure is not None else {}
            ),
        })
        manifest_uri = f"epoch://{self.epoch_id}/{manifest.relative_to(self.epoch_root).as_posix()}"
        hashes[manifest_uri] = file_digest(manifest)
        return hashes

    def verify(self, hashes: dict[str, str]) -> tuple[bool, dict[str, str]]:
        observed: dict[str, str] = {}
        for uri in hashes:
            try:
                observed[uri] = file_digest(self.resolve_uri(uri))
            except ValueError:
                continue
        return observed == hashes, observed

    def materialize(self, uris: Iterable[str], target_root: Path) -> list[Path]:
        target = target_root.resolve()
        copied: list[Path] = []
        for uri in uris:
            source = self.resolve_uri(uri)
            digest = file_digest(source)
            destination = target / "candidate_bundle" / digest / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
            copied.append(destination)
        return copied
