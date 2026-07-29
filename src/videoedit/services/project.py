from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoedit.errors import StateConflictError

PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")
REVISION_ID_PATTERN = re.compile(r"^rev_[0-9]{3,}$")
DIRECTORIES = (
    "raw",
    "config",
    "artifacts",
    "work",
    "review",
    "output",
    "logs",
    "state",
    "revisions",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_text_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve())

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def review(self) -> Path:
        return self.root / "review"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def revisions(self) -> Path:
        return self.root / "revisions"

    @property
    def lock_path(self) -> Path:
        return self.state / "project.lock"

    @property
    def stage_state(self) -> Path:
        return self.state / "stages"

    @property
    def staging(self) -> Path:
        return self.state / "staging"

    def revision_root(self, revision_id: str) -> Path:
        if not REVISION_ID_PATTERN.fullmatch(revision_id):
            raise ValueError(f"invalid revision_id: {revision_id}")
        return self.revisions / revision_id


class ProjectLock:
    """Exclusive project mutation lock with owner and heartbeat evidence."""

    def __init__(
        self,
        layout: ProjectLayout,
        *,
        stage: str,
        revision_id: str = "rev_001",
        owner_id: str | None = None,
        stale_after_seconds: float = 900.0,
    ) -> None:
        self.layout = layout
        self.stage = stage
        self.revision_id = revision_id
        self.owner_id = owner_id or f"{socket.gethostname()}:{os.getpid()}"
        self.stale_after_seconds = stale_after_seconds
        self.lock_id = uuid.uuid4().hex
        self._held = False

    def __enter__(self) -> ProjectLock:
        self.layout.state.mkdir(parents=True, exist_ok=True)
        payload = self._payload()
        try:
            descriptor = os.open(
                self.layout.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            if not self._recoverable_stale_lock():
                raise StateConflictError(
                    f"project is locked for stage {self.stage}: {self.layout.lock_path}"
                ) from None
            descriptor = os.open(
                self.layout.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            self.layout.lock_path.unlink(missing_ok=True)
            raise
        self._held = True
        return self

    def heartbeat(self) -> None:
        if not self._held:
            raise StateConflictError("project lock is not held by this process")
        current = self._read_lock()
        if current.get("lock_id") != self.lock_id:
            raise StateConflictError("project lock ownership changed during the stage")
        current["heartbeat_at"] = _now_iso()
        _atomic_json_write(self.layout.lock_path, current)

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if not self._held:
            return
        try:
            current = self._read_lock()
            if current.get("lock_id") == self.lock_id:
                self.layout.lock_path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        finally:
            self._held = False

    def _payload(self) -> dict[str, Any]:
        timestamp = _now_iso()
        return {
            "schema_name": "project_lock",
            "schema_version": "1.0.0",
            "lock_id": self.lock_id,
            "owner_id": self.owner_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "stage": self.stage,
            "revision_id": self.revision_id,
            "created_at": timestamp,
            "heartbeat_at": timestamp,
        }

    def _read_lock(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.layout.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateConflictError(
                "project lock is unreadable and requires operator recovery: "
                f"{self.layout.lock_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise StateConflictError("project lock must contain a JSON object")
        return payload

    def _recoverable_stale_lock(self) -> bool:
        payload = self._read_lock()
        heartbeat = payload.get("heartbeat_at")
        hostname = payload.get("hostname")
        pid = payload.get("pid")
        if not isinstance(heartbeat, str) or hostname != socket.gethostname():
            return False
        try:
            heartbeat_time = datetime.fromisoformat(heartbeat)
            age_seconds = (datetime.now(UTC) - heartbeat_time).total_seconds()
        except ValueError:
            return False
        if age_seconds <= self.stale_after_seconds:
            return False
        if not isinstance(pid, int) or _process_alive(pid):
            return False
        stale_name = self.layout.lock_path.with_name(
            f"{self.layout.lock_path.name}.stale.{payload.get('lock_id', uuid.uuid4().hex)}"
        )
        os.replace(self.layout.lock_path, stale_name)
        return True


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def initialize_project(workspace: Path, project_id: str) -> ProjectLayout:
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise ValueError(
            "project_id must match ^[a-z][a-z0-9_-]{2,127}$ and contain no path separators"
        )
    workspace = workspace.expanduser().resolve()
    layout = ProjectLayout(workspace / "projects" / project_id)
    layout.root.mkdir(parents=True, exist_ok=True)
    with ProjectLock(layout, stage="project_init"):
        for directory in DIRECTORIES:
            (layout.root / directory).mkdir(parents=True, exist_ok=True)
        revision_root = layout.revision_root("rev_001")
        revision_root.mkdir(parents=True, exist_ok=True)
        revision_file = revision_root / "revision.json"
        if not revision_file.exists():
            _atomic_json_write(
                revision_file,
                {
                    "schema_name": "project_revision",
                    "schema_version": "1.0.0",
                    "project_id": project_id,
                    "revision_id": "rev_001",
                    "parent_revision_id": None,
                    "created_at": _now_iso(),
                    "active": True,
                    "directories": {
                        "artifacts": str(layout.artifacts),
                        "review": str(layout.review),
                        "work": str(layout.work),
                        "output": str(layout.output),
                    },
                },
            )
        project_file = layout.config / "project.yaml"
        if not project_file.exists():
            _atomic_text_write(
                project_file,
                yaml_safe_dump(
                    {
                        "schema_version": "1.0",
                        "project_id": project_id,
                        "recording_mode": "screen_recording",
                        "width": 1920,
                        "height": 1080,
                        "fps": 30,
                        "review_gates": ["plan", "segments", "final"],
                    }
                ),
            )
        manifest_file = layout.state / "project-manifest.json"
        if not manifest_file.exists():
            timestamp = _now_iso()
            payload = {
                "schema_name": "project_manifest",
                "schema_version": "1.0.0",
                "project_id": project_id,
                "active_revision_id": "rev_001",
                "name": project_id.replace("_", " ").replace("-", " "),
                "created_at": timestamp,
                "updated_at": timestamp,
                "data_classification": "internal",
                "source_mode": "copy",
                "state": "created",
                "configuration": {
                    "path": str(project_file),
                    "sha256": sha256_file(project_file),
                },
                "source_artifact_id": None,
                "active_artifacts": {},
            }
            _atomic_json_write(manifest_file, payload)
    return layout


def yaml_safe_dump(payload: dict[str, object]) -> str:
    # Keep the YAML dependency at this small project-configuration boundary.
    import yaml

    return yaml.safe_dump(payload, sort_keys=False)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def ingest_source(
    layout: ProjectLayout,
    source: Path,
    package_root: Path | None = None,
    adapter: object | None = None,
    copy_source: bool = True,
) -> dict[str, object]:
    """Compatibility wrapper around the schema-valid ingest and probe service."""

    from videoedit.services.media import ingest_and_probe

    resolved_root = package_root or Path(__file__).resolve().parents[3]
    return ingest_and_probe(
        resolved_root,
        layout,
        source,
        adapter=adapter,
        copy_source=copy_source,
    )
