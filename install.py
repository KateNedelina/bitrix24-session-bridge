#!/usr/bin/env python3
"""Install or atomically upgrade bitrix24-session-bridge while preserving .env."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from installation_integrity import (  # noqa: E402
    SKILL_NAME,
    build_install_marker,
    discover_skill_copies,
)


ROOT = Path(__file__).resolve().parent
INSTALL_MARKER = ".bitrix24-session-bridge-release.json"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")


def configure_utf8_console() -> None:
    """Keep Russian diagnostics printable on Windows consoles with legacy encodings."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def release_version() -> str:
    value = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(value):
        raise RuntimeError(f"Некорректная версия: {value!r}")
    return value


def codex_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def ignore_source(_directory: str, names: list[str]) -> set[str]:
    ignored = {".git", "__pycache__", ".DS_Store", ".env", "bitrix24_company_contexts", "raw", "documents", "metadata"}
    return {name for name in names if name in ignored or name.endswith((".pyc", ".pyo"))}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True, capture_output=True, timeout=5, check=False,
        )
    except Exception:
        return None
    return (completed.stdout.strip() or None) if completed.returncode == 0 else None


def copy_release(target: Path, *, installed_at: str, previous_active_backup: str | None,
                 preserved_env: Path | None) -> str | None:
    shutil.copytree(ROOT, target, ignore=ignore_source)
    preserved_env_sha256 = None
    if preserved_env is not None:
        shutil.copy2(preserved_env, target / ".env")
        preserved_env_sha256 = sha256(target / ".env")
    marker = build_install_marker(
        target,
        version=release_version(),
        installed_at=installed_at,
        source_commit=source_commit(),
        previous_active_backup=previous_active_backup,
        preserved_env_sha256=preserved_env_sha256,
    )
    (target / INSTALL_MARKER).write_text(
        json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return preserved_env_sha256


def quarantine_discoverable_copies(skills_root: Path, target: Path, backup_session: Path) -> list[dict[str, str]]:
    roots: list[Path] = []
    for candidate in discover_skill_copies(skills_root, exclude=target):
        if not any(candidate.is_relative_to(existing) for existing in roots):
            roots.append(candidate)
    relocations: list[dict[str, str]] = []
    for index, source in enumerate(roots, 1):
        destination = backup_session / "quarantined" / f"{index:03d}-{source.name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        relocations.append({"original_path": str(source), "backup_path": str(destination)})
    return relocations


def restore_quarantined_copies(relocations: list[dict[str, str]]) -> None:
    for item in reversed(relocations):
        original = Path(item["original_path"])
        backup = Path(item["backup_path"])
        if not backup.exists():
            continue
        if original.exists():
            raise RuntimeError(f"Не удалось восстановить резервную копию: путь уже занят: {original}")
        original.parent.mkdir(parents=True, exist_ok=True)
        backup.replace(original)


def verify_active(target: Path, report: Path) -> None:
    command = [
        sys.executable,
        str(target / "scripts" / "verify_release.py"),
        "--skill-dir", str(target),
        "--require-install-marker",
        "--output", str(report),
    ]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(command, text=True, capture_output=True, env=environment)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError("Активная установка моста не прошла проверку:\n" + detail[-4000:])


def main() -> int:
    configure_utf8_console()
    parser = argparse.ArgumentParser(description="Установить bitrix24-session-bridge")
    parser.add_argument("--upgrade", action="store_true", help="атомарно заменить установленную версию, сохранив .env")
    args = parser.parse_args()

    home = codex_root()
    skills_root = home / "skills"
    target = skills_root / SKILL_NAME
    report = home / f"{SKILL_NAME}-installation-validation.json"
    skills_root.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.upgrade:
        raise SystemExit("Целевой скилл уже установлен. Используйте python install.py --upgrade")
    if args.upgrade and not target.exists():
        raise SystemExit("Обновление остановлено: предыдущая установка не найдена")

    installed_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    session_name = installed_at.replace(":", "").replace("+", "-") + "-" + uuid.uuid4().hex[:8]
    backup_session = home / "backups" / SKILL_NAME / session_name
    stage_parent = home / ".staging"
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=f"{SKILL_NAME}-", dir=stage_parent))
    staged = stage_root / SKILL_NAME
    previous_active = backup_session / "previous-active"
    preserved_env = target / ".env" if (target / ".env").is_file() else None
    env_sha_before = sha256(preserved_env) if preserved_env else None
    activated = False
    quarantined: list[dict[str, str]] = []
    try:
        if target.exists() or discover_skill_copies(skills_root, exclude=target):
            backup_session.mkdir(parents=True, exist_ok=True)
            quarantined = quarantine_discoverable_copies(skills_root, target, backup_session)
        env_sha_after = copy_release(
            staged,
            installed_at=installed_at,
            previous_active_backup=str(previous_active) if target.exists() else None,
            preserved_env=preserved_env,
        )
        if env_sha_before != env_sha_after:
            raise RuntimeError("SHA-256 файла .env изменился во время подготовки обновления")
        if target.exists():
            target.replace(previous_active)
        staged.replace(target)
        activated = True
        verify_active(target, report)
        remaining = discover_skill_copies(skills_root, exclude=target)
        if remaining:
            raise RuntimeError("После установки остались обнаруживаемые дубликаты: " + ", ".join(map(str, remaining)))
        payload = json.loads(report.read_text(encoding="utf-8"))
        payload["installation"] = {
            "created_at": installed_at,
            "active_path": str(target),
            "previous_active_backup": str(previous_active) if previous_active.exists() else None,
            "backup_session": str(backup_session) if backup_session.exists() else None,
            "quarantined_discoverable_copies": quarantined,
            "env_sha256_before": env_sha_before,
            "env_sha256_after": sha256(target / ".env") if (target / ".env").is_file() else None,
            "env_preserved": env_sha_before == (sha256(target / ".env") if (target / ".env").is_file() else None),
        }
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        if activated and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if previous_active.exists():
            previous_active.replace(target)
        restore_quarantined_copies(quarantined)
        shutil.rmtree(backup_session, ignore_errors=True)
        failure = {
            "schema_version": "1.0", "skill": SKILL_NAME, "version": release_version(),
            "status": "FAIL", "error": str(exc), "rolled_back": True,
            "active_path": str(target),
            "env_sha256_before": env_sha_before,
            "env_sha256_after": sha256(target / ".env") if (target / ".env").is_file() else None,
        }
        report.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    print(f"{SKILL_NAME} v{release_version()} установлен и проверен: {target}")
    print(f"Отчёт: {report}")
    if previous_active.exists() or quarantined:
        print(f"Резервные копии вынесены из каталога skills: {backup_session}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
