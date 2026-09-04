#!/usr/bin/env python3
"""Fast, credential-free health check for a Bitrix Session Bridge directory."""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys


def check(condition: bool, label: str, failures: list[str]) -> None:
    print(f"{'PASS' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


def main() -> int:
    parser = argparse.ArgumentParser(description="Быстрая credential-free проверка bitrix24-session-bridge.")
    parser.add_argument("--skill-dir", default=str(pathlib.Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    root = pathlib.Path(args.skill_dir).expanduser().resolve()
    failures: list[str] = []
    script = root / "scripts" / "bitrix24_session_client.py"
    for relative in ("SKILL.md", "README.md", "scripts/bitrix24_session_client.py"):
        check((root / relative).is_file(), f"file:{relative}", failures)
    help_result = subprocess.run([sys.executable, str(script), "--help"], text=True, capture_output=True)
    check(help_result.returncode == 0 and "collect-project-folder" in help_result.stdout, "bridge_help", failures)
    contract_result = subprocess.run([sys.executable, str(script), "contract"], text=True, capture_output=True)
    try:
        contract = json.loads(contract_result.stdout)
    except json.JSONDecodeError:
        contract = {}
    check(contract_result.returncode == 0 and contract.get("read_only") is True, "read_only_contract", failures)
    required = {"field_schema_export", "reference_display_value_resolution", "project_folder_inventory"}
    check(required <= set(contract.get("capabilities", [])), "contract_capabilities", failures)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
