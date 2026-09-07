#!/usr/bin/env python3
"""Installation identity and fingerprint checks for bitrix24-session-bridge."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any


SKILL_NAME = "bitrix24-session-bridge"
INSTALL_MARKER = ".bitrix24-session-bridge-release.json"
FINGERPRINT_FILES = (
    "VERSION",
    "SKILL.md",
    "README.md",
    "install.py",
    "scripts/bitrix24_session_client.py",
    "scripts/quick_validate.py",
    "scripts/installation_integrity.py",
    "scripts/verify_release.py",
)
_NAME_PATTERN = re.compile(r"(?m)^name:\s*['\"]?([^'\"\s]+)")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configured_codex_root() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser().resolve() if raw else (Path.home() / ".codex").resolve()


def _path_parts(value: str | Path, *, windows: bool) -> tuple[str, ...]:
    path_type = PureWindowsPath if windows else PurePosixPath
    parts = path_type(str(value)).parts
    return tuple(part.casefold() for part in parts) if windows else tuple(parts)


def classify_skill_location(skill_dir: str | Path, codex_root: str | Path, *,
                            windows: bool | None = None) -> str:
    use_windows = os.name == "nt" if windows is None else windows
    separator = "\\skills" if use_windows else "/skills"
    skill_parts = _path_parts(skill_dir, windows=use_windows)
    skills_parts = _path_parts(str(codex_root) + separator, windows=use_windows)
    if skill_parts[:len(skills_parts)] != skills_parts:
        return "DEVELOPMENT_CHECKOUT"
    relative = skill_parts[len(skills_parts):]
    expected = SKILL_NAME.casefold() if use_windows else SKILL_NAME
    return "ACTIVE_INSTALL" if relative == (expected,) else "STALE_SKILL_COPY"


def skill_name_from_manifest(path: Path) -> str | None:
    try:
        match = _NAME_PATTERN.search(path.read_text(encoding="utf-8", errors="ignore")[:8192])
    except OSError:
        return None
    return match.group(1).strip() if match else None


def discover_skill_copies(skills_root: Path, *, exclude: Path | None = None) -> list[Path]:
    root = skills_root.expanduser().resolve()
    excluded = exclude.expanduser().resolve() if exclude is not None else None
    copies: list[Path] = []
    if not root.is_dir():
        return copies
    for manifest in root.rglob("SKILL.md"):
        if manifest.is_symlink() or skill_name_from_manifest(manifest) != SKILL_NAME:
            continue
        candidate = manifest.parent.resolve()
        if excluded is None or candidate != excluded:
            copies.append(candidate)
    return sorted(set(copies), key=lambda item: (len(item.parts), str(item).casefold()))


def content_fingerprint(skill_dir: Path) -> dict[str, Any]:
    root = skill_dir.expanduser().resolve()
    files = {
        relative: file_hash(root / relative) if (root / relative).is_file() else None
        for relative in FINGERPRINT_FILES
    }
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": "1.0",
        "algorithm": "sha256-key-files-v1",
        "files": files,
        "content_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def build_install_marker(skill_dir: Path, *, version: str, installed_at: str,
                         source_commit: str | None, previous_active_backup: str | None,
                         preserved_env_sha256: str | None) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "skill": SKILL_NAME,
        "version": version,
        "installed_at": installed_at,
        "source_commit": source_commit,
        "fingerprint": content_fingerprint(skill_dir),
        "previous_active_backup": previous_active_backup,
        "preserved_env_sha256": preserved_env_sha256,
    }


def install_marker_errors(skill_dir: Path, marker: dict[str, Any] | None = None) -> list[str]:
    root = skill_dir.expanduser().resolve()
    errors: list[str] = []
    if marker is None:
        try:
            marker = json.loads((root / INSTALL_MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return [f"marker:{exc}"]
    if not isinstance(marker, dict):
        return ["marker:not_an_object"]
    version = (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").is_file() else None
    if marker.get("skill") != SKILL_NAME:
        errors.append("marker.skill")
    if marker.get("version") != version:
        errors.append("marker.version")
    actual = content_fingerprint(root)
    declared = marker.get("fingerprint")
    if not isinstance(declared, dict):
        return [*errors, "marker.fingerprint:not_an_object"]
    if declared.get("algorithm") != actual["algorithm"]:
        errors.append("marker.fingerprint.algorithm")
    if declared.get("files") != actual["files"]:
        errors.append("marker.fingerprint.files")
    if declared.get("content_sha256") != actual["content_sha256"]:
        errors.append("marker.fingerprint.content_sha256")
    if any(value is None for value in actual["files"].values()):
        errors.append("fingerprint.required_file_missing")
    return errors


def installation_identity(skill_dir: Path, *, codex_root: Path | None = None) -> dict[str, Any]:
    root = skill_dir.expanduser().resolve()
    home = (codex_root or configured_codex_root()).expanduser().resolve()
    location = classify_skill_location(root, home)
    marker_path = root / INSTALL_MARKER
    duplicates: list[str] = []
    marker_errors: list[str] = []
    if location == "ACTIVE_INSTALL":
        duplicates = [str(path) for path in discover_skill_copies(home / "skills", exclude=root)]
        marker_errors = install_marker_errors(root)
    status = "PASS"
    if location == "STALE_SKILL_COPY" or duplicates or marker_errors:
        status = "BLOCKED"
    return {
        "status": status,
        "location_status": location,
        "skill_dir": str(root),
        "expected_active_path": str((home / "skills" / SKILL_NAME).resolve()),
        "discoverable_duplicates": duplicates,
        "marker_path": str(marker_path.resolve()),
        "marker_present": marker_path.is_file(),
        "marker_errors": marker_errors,
        "fingerprint": content_fingerprint(root),
    }
