from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from installation_integrity import classify_skill_location  # noqa: E402
sys.path.insert(0, str(ROOT))
import install as installer  # noqa: E402


class InstallationTests(unittest.TestCase):
    def test_windows_active_and_nested_paths_are_distinguished(self):
        home = r"C:\Users\fixture-user\.codex"
        self.assertEqual(
            classify_skill_location(r"C:\Users\fixture-user\.codex\skills\bitrix24-session-bridge", home, windows=True),
            "ACTIVE_INSTALL",
        )
        self.assertEqual(
            classify_skill_location(
                r"C:\Users\fixture-user\.codex\skills\backups-old\bitrix24-session-bridge",
                home,
                windows=True,
            ),
            "STALE_SKILL_COPY",
        )

    def test_upgrade_preserves_env_and_writes_verified_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp) / ".codex"
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(home)
            first = subprocess.run([sys.executable, str(ROOT / "install.py")], env=environment, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            target = home / "skills" / "bitrix24-session-bridge"
            env_path = target / ".env"
            env_path.write_text("B24_BASE_URL=https://example.invalid\nB24_LOGIN=test\nB24_PASSWORD=secret\n", encoding="utf-8")
            before = hashlib.sha256(env_path.read_bytes()).hexdigest()
            upgraded = subprocess.run(
                [sys.executable, str(ROOT / "install.py"), "--upgrade"],
                env=environment, text=True, capture_output=True,
            )
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr or upgraded.stdout)
            self.assertEqual(hashlib.sha256(env_path.read_bytes()).hexdigest(), before)
            marker = json.loads((target / ".bitrix24-session-bridge-release.json").read_text(encoding="utf-8"))
            report = json.loads((home / "bitrix24-session-bridge-installation-validation.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["version"], "0.3.1")
            self.assertEqual(report["status"], "PASS")
            self.assertTrue(report["installation"]["env_preserved"])

    def test_failed_upgrade_restores_previous_active_copy_and_env(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp) / ".codex"
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(home)
            first = subprocess.run([sys.executable, str(ROOT / "install.py")], env=environment, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            target = home / "skills" / "bitrix24-session-bridge"
            env_path = target / ".env"
            env_path.write_text("B24_BASE_URL=https://example.invalid\nB24_LOGIN=test\nB24_PASSWORD=secret\n", encoding="utf-8")
            before = hashlib.sha256(env_path.read_bytes()).hexdigest()
            with patch.dict(os.environ, {"CODEX_HOME": str(home)}), \
                    patch.object(sys, "argv", ["install.py", "--upgrade"]), \
                    patch.object(installer, "verify_active", side_effect=RuntimeError("synthetic validation failure")):
                with self.assertRaisesRegex(RuntimeError, "synthetic validation failure"):
                    installer.main()
            self.assertTrue(target.is_dir())
            self.assertEqual(hashlib.sha256(env_path.read_bytes()).hexdigest(), before)
            report = json.loads((home / "bitrix24-session-bridge-installation-validation.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "FAIL")
            self.assertTrue(report["rolled_back"])
            self.assertEqual(report["env_sha256_before"], report["env_sha256_after"])


if __name__ == "__main__":
    unittest.main()
