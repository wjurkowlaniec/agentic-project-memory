from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _atomic_json(path: Path, payload: dict[str, object], mode: int = 0o600) -> None:
    _secure_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        path.chmod(mode)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    root: Path
    aliases: tuple[Path, ...] = ()
    source_locations: dict[str, str] = field(default_factory=dict)
    model_names: dict[str, str] = field(default_factory=dict)
    prompt_version: int = 1
    schema_version: int = 1
    redaction_version: int = 1

    @property
    def project_id(self) -> str:
        canonical = Path(self.root).expanduser().resolve()
        return hashlib.sha256(str(canonical).encode()).hexdigest()[:16]

    @classmethod
    def create(cls, name: str, root: Path, aliases: list[Path] | tuple[Path, ...] = ()) -> "ProjectConfig":
        return cls(name=name, root=Path(root).expanduser().resolve(), aliases=tuple(Path(p).expanduser().resolve() for p in aliases))

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["root"] = str(self.root)
        data["aliases"] = [str(p) for p in self.aliases]
        return data

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "ProjectConfig":
        return cls(
            name=str(data["name"]),
            root=Path(str(data["root"])).expanduser().resolve(),
            aliases=tuple(Path(str(path)).expanduser().resolve() for path in data.get("aliases", [])),
            source_locations={str(k): str(v) for k, v in dict(data.get("source_locations", {})).items()},
            model_names={str(k): str(v) for k, v in dict(data.get("model_names", {})).items()},
            prompt_version=int(data.get("prompt_version", 1)),
            schema_version=int(data.get("schema_version", 1)),
            redaction_version=int(data.get("redaction_version", 1)),
        )


@dataclass(frozen=True)
class ProjectPaths:
    data_home: Path
    project_id: str
    project_dir: Path
    config_file: Path
    vault_db: Path
    memory_db: Path
    receipts_dir: Path
    exports_dir: Path

    @classmethod
    def for_root(cls, root: Path, data_home: Path | None = None) -> "ProjectPaths":
        canonical = Path(root).expanduser().resolve()
        base = (Path(data_home).expanduser() if data_home is not None else Path.home() / ".project-memory").resolve()
        _secure_dir(base); _secure_dir(base / "projects")
        project_id = _project_id_for(canonical, base)
        project_dir = _secure_dir(base / "projects" / project_id)
        return cls(base, project_id, project_dir, project_dir / "config.json", project_dir / "vault.sqlite3", project_dir / "memory.sqlite3", _secure_dir(project_dir / "receipts"), _secure_dir(project_dir / "exports"))


def _hash_root(root: Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


def _read_registry(base: Path) -> dict[str, dict[str, object]]:
    path = base / "registry.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data.get("projects", {}))
    except (OSError, ValueError, TypeError):
        raise ValueError("invalid_project_registry")


def _project_id_for(root: Path, base: Path) -> str:
    for project_id, entry in _read_registry(base).items():
        roots = [entry.get("root"), *(entry.get("aliases", []) or [])]
        if str(root) in {str(Path(value).expanduser().resolve()) for value in roots if value}:
            return str(project_id)
    return _hash_root(root)


def _write_registry(base: Path, config: ProjectConfig) -> None:
    registry = _read_registry(base)
    registry[config.project_id] = {"root": str(config.root), "aliases": [str(p) for p in config.aliases]}
    _atomic_json(base / "registry.json", {"projects": registry})


def save_project_config(config: ProjectConfig, paths: ProjectPaths) -> None:
    _secure_dir(paths.data_home)
    registry = _read_registry(paths.data_home)
    own_ids = {config.project_id}
    requested = {str(config.root), *(str(alias) for alias in config.aliases)}
    for project_id, entry in registry.items():
        if str(project_id) in own_ids:
            continue
        occupied = {str(Path(value).expanduser().resolve()) for value in [entry.get("root"), *(entry.get("aliases", []) or [])] if value}
        collision = sorted(requested & occupied)
        if collision:
            raise ValueError("project_path_collision")
    _atomic_json(paths.config_file, config.to_json())
    registry[config.project_id] = {"root": str(config.root), "aliases": [str(p) for p in config.aliases]}
    _atomic_json(paths.data_home / "registry.json", {"projects": registry})


def load_project_config(root: Path, data_home: Path | None = None) -> ProjectConfig:
    base = (Path(data_home).expanduser() if data_home is not None else Path.home() / ".project-memory").resolve()
    requested = Path(root).expanduser().resolve()
    project_id = _project_id_for(requested, base)
    project_file = base / "projects" / project_id / "config.json"
    legacy_file = base / "config.json"
    if not project_file.exists() and legacy_file.exists():
        try:
            legacy = ProjectConfig.from_json(json.loads(legacy_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError("invalid_project_config") from exc
        if requested not in {legacy.root, *legacy.aliases}:
            raise ValueError(f"unregistered project root: {requested}")
        save_project_config(legacy, ProjectPaths.for_root(legacy.root, base))
        project_file = base / "projects" / legacy.project_id / "config.json"
    if not project_file.exists():
        raise ValueError(f"unregistered project root: {requested}")
    try:
        config = ProjectConfig.from_json(json.loads(project_file.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("invalid_project_config") from exc
    if requested not in {config.root, *config.aliases}:
        raise ValueError(f"unregistered project root: {requested}")
    return config
