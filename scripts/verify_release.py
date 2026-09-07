#!/usr/bin/env python3
"""Credential-free release validation for bitrix24-session-bridge."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys

from installation_integrity import INSTALL_MARKER, installation_identity, install_marker_errors


EXPECTED_VERSION = "0.3.1"
EXPECTED_CONTRACT = "1.2"
EXPECTED_CAPABILITY = "exact_contact_related_list_collection"


def run(command: list[str], cwd: Path) -> tuple[bool, str]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    detail = (completed.stdout + "\n" + completed.stderr).strip()
    return completed.returncode == 0, detail[-4000:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить релиз bitrix24-session-bridge")
    parser.add_argument("--skill-dir", required=True)
    parser.add_argument("--require-install-marker", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.skill_dir).expanduser().resolve()
    checks: list[dict[str, str]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    required = (
        "VERSION", "SKILL.md", "README.md", "install.py",
        "scripts/bitrix24_session_client.py", "scripts/quick_validate.py",
        "scripts/installation_integrity.py", "scripts/verify_release.py",
        "scripts/test_collect_deal_context.py", "scripts/test_income_contract_list.py",
    )
    missing = [relative for relative in required if not (root / relative).is_file()]
    check("release_components", not missing, "Все обязательные файлы есть" if not missing else ", ".join(missing))

    version = (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").is_file() else ""
    check("version", version == EXPECTED_VERSION and bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), version or "VERSION отсутствует")

    syntax_errors: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            syntax_errors.append(f"{path.relative_to(root)}: {exc}")
    check("python_syntax", not syntax_errors, "Python-файлы читаются" if not syntax_errors else "; ".join(syntax_errors))

    ok, detail = run([sys.executable, str(root / "scripts" / "quick_validate.py"), "--skill-dir", str(root)], root)
    check("quick_validation", ok, detail or "PASS")
    ok, detail = run([sys.executable, "-m", "unittest", "discover", "-s", "scripts", "-p", "test_*.py", "-v"], root)
    check("bridge_tests", ok, detail or "PASS")

    client = root / "scripts" / "bitrix24_session_client.py"
    ok, detail = run([sys.executable, str(client), "contract"], root)
    try:
        contract = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        contract = {}
    contract_ok = (
        ok
        and contract.get("contract_version") == EXPECTED_CONTRACT
        and contract.get("read_only") is True
        and EXPECTED_CAPABILITY in contract.get("capabilities", [])
    )
    check("controller_contract", contract_ok, detail or "Контракт не получен")

    identity = None
    if args.require_install_marker:
        identity = installation_identity(root)
        check("canonical_active_install", identity.get("location_status") == "ACTIVE_INSTALL", str(identity.get("location_status")))
        check("single_discoverable_skill_copy", not identity.get("discoverable_duplicates"), str(identity.get("discoverable_duplicates")))
        try:
            marker = json.loads((root / INSTALL_MARKER).read_text(encoding="utf-8"))
            marker_errors = install_marker_errors(root, marker)
        except (OSError, ValueError, TypeError) as exc:
            marker_errors = [str(exc)]
        check("install_marker", not marker_errors, "Fingerprint подтверждён" if not marker_errors else ", ".join(marker_errors))

    result = {
        "schema_version": "1.0",
        "skill": "bitrix24-session-bridge",
        "version": version,
        "status": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
        "installation_identity": identity,
        "checks": checks,
    }
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    if result["status"] != "PASS":
        for item in checks:
            if item["status"] == "FAIL":
                print(f"FAIL {item['check']}: {item['detail']}", file=sys.stderr)
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
