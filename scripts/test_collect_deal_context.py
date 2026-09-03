#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("bitrix24_session_client.py")
SPEC = importlib.util.spec_from_file_location("bitrix24_session_client", MODULE_PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FakeClient:
    base_url = "https://crm.example.test"

    def login_portal(self):
        return self.base_url + "/stream/"

    def fetch(self, target):
        return (
            self.base_url + "/crm/deal/details/555/",
            """
            <html><body>
            <script>window.card = { entityTypeId: 2, data: {"ID":"555","TITLE":"5555: Synthetic","ASSIGNED_BY_FORMATTED_NAME":"Account Test","UF_ALPHA":{"VALUE":"x"}} };</script>
            <a href="/crm/company/details/777/">Synthetic Company</a>
            </body></html>
            """,
        )


class OuterShellClient(FakeClient):
    def __init__(self):
        self.targets = []

    def fetch(self, target):
        self.targets.append(target)
        if "IFRAME=Y" not in target:
            return self.base_url + "/crm/deal/details/555/", "<html><body>portal shell</body></html>"
        return (
            self.base_url + "/crm/deal/details/555/?IFRAME=Y&IFRAME_TYPE=SIDE_SLIDER",
            """
            <script>
            window.fields = {"UF_ALPHA":{"title":"Вводные данные","type":"string","multiple":false}};
            window.card = { entityTypeId: 2, data: {"ID":"555","TITLE":"5555: Synthetic","UF_ALPHA":""} };
            </script>
            """,
        )


class WrongDealClient(FakeClient):
    def fetch(self, target):
        return (
            self.base_url + "/crm/deal/details/555/",
            '<script>window.card = { entityTypeId: 2, data: {"ID":"999","TITLE":"Other"} };</script>',
        )


class ExactEntityClient(FakeClient):
    def __init__(self):
        self.login_calls = 0

    def login_portal(self):
        self.login_calls += 1
        return super().login_portal()

    def fetch(self, target):
        return (
            self.base_url + "/crm/contact/details/42/",
            '<script>window.card = { entityTypeId: 3, data: {"ID":"42","LAST_NAME":"Тест","NAME":"Иван","SECOND_NAME":"Иванович"} };</script>',
        )


class FolderClient(FakeClient):
    def fetch(self, target):
        if "child-folder" in target:
            return (
                self.base_url + "/docs/shared/path/root/child-folder/",
                '"id":"2","name":"notes.txt","isFolder":false,"href":"\\/disk\\/downloadFile\\/2\\/?filename=notes.txt"',
            )
        return (
            self.base_url + "/docs/shared/path/root/",
            '"id":"1","name":"proof.png","isFolder":false,"href":"\\/disk\\/downloadFile\\/1\\/?filename=proof.png" '
            '<a href="/docs/shared/path/root/child-folder/">child-folder</a>',
        )

    def fetch_binary(self, target):
        return target, b"\x89PNG\r\n\x1a\nsynthetic"


class CollectDealContextTests(unittest.TestCase):
    def test_direct_deal_id_writes_machine_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            code = bridge.command_collect_deal_context(
                FakeClient(), temp_dir, None, "555", None, True
            )
            self.assertEqual(code, 0)
            meta = pathlib.Path(temp_dir) / "metadata"
            report = json.loads((meta / "run_report.json").read_text(encoding="utf-8"))
            fields = json.loads((meta / "fields.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["selected_deal"]["deal_id"], "555")
            self.assertTrue(any(item["field_code"] == "UF_ALPHA" for item in fields))
            self.assertTrue((pathlib.Path(temp_dir) / "raw" / "deal.html").exists())

    def test_invalid_url_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            code = bridge.command_collect_deal_context(
                FakeClient(), temp_dir, None, None, "https://crm.example.test/crm/contact/details/1/", True
            )
            self.assertEqual(code, 2)
            report = json.loads(
                (pathlib.Path(temp_dir) / "metadata" / "run_report.json").read_text(encoding="utf-8")
            )
            self.assertIn("DEAL_URL_INVALID", report["errors"])

    def test_outer_shell_retries_side_slider_and_preserves_empty_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = OuterShellClient()
            code = bridge.command_collect_deal_context(client, temp_dir, None, "555", None, True)
            self.assertEqual(code, 0)
            meta = pathlib.Path(temp_dir) / "metadata"
            fields = json.loads((meta / "fields.json").read_text(encoding="utf-8"))
            alpha = next(item for item in fields if item["field_code"] == "UF_ALPHA")
            self.assertEqual(alpha["availability"], "FIELD_EMPTY")
            self.assertEqual(alpha["field_title"], "Вводные данные")
            self.assertTrue(alpha["source_url"].endswith("IFRAME=Y&IFRAME_TYPE=SIDE_SLIDER"))
            self.assertEqual(len(client.targets), 2)

    def test_wrong_deal_model_never_substitutes_for_expected_deal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            code = bridge.command_collect_deal_context(WrongDealClient(), temp_dir, None, "555", None, True)
            self.assertEqual(code, 2)
            report = json.loads(
                (pathlib.Path(temp_dir) / "metadata" / "run_report.json").read_text(encoding="utf-8")
            )
            self.assertIn("DEAL_EXACT_MACHINE_MODEL_NOT_UNIQUE", report["errors"])

    def test_entity_editor_current_config_supplies_title_type_and_enum_version(self):
        html = """<script>BX.editor({current: [{'name':'UF_FORMAT','type':'enumeration','title':'Формат занятости',
        'data':{'fieldInfo':{'USER_TYPE_ID':'enumeration','MULTIPLE':'N','ENUM':[{'ID':'7','VALUE':'Гибрид'}]}}}]});</script>"""
        schema = bridge.extract_field_schema(html, {"UF_FORMAT": "7"})["UF_FORMAT"]
        self.assertEqual(schema["field_title"], "Формат занятости")
        self.assertEqual(schema["field_type"], "enumeration")
        self.assertEqual(schema["enumeration_options"], {"7": "Гибрид"})
        self.assertTrue(schema["option_list_version"].startswith("sha256:"))

    def test_standard_title_has_schema_metadata_and_empty_editor_field_is_exported(self):
        html = """<script>BX.editor({current: [{'name':'UF_EMPTY','type':'string','title':'Пустое поле',
        'data':{'fieldInfo':{'USER_TYPE_ID':'string','MULTIPLE':'N'}}}]});</script>"""
        schema = bridge.extract_field_schema(html, {"TITLE": "4668: Synthetic"})
        self.assertEqual(schema["TITLE"]["field_title"], "Название")
        self.assertEqual(schema["TITLE"]["metadata_source"], "STANDARD_BITRIX_FIELD_SCHEMA")
        self.assertIn("UF_EMPTY", schema)
        fields = bridge.build_field_records(
            schema, {"TITLE": "4668: Synthetic"}, entity_type="deal", entity_type_id="2", entity_id="1",
            source_url="https://crm.example.test/crm/deal/details/1/", read_at="2030-01-01T00:00:00+00:00",
            retrieval_method="TEST",
        )
        empty = next(item for item in fields if item["field_code"] == "UF_EMPTY")
        self.assertEqual(empty["availability"], "FIELD_EMPTY")

    def test_contact_and_company_paths_and_entity_login_are_supported(self):
        self.assertEqual(bridge.classify_entity_path("/crm/contact/details/42/")["kind"], "contact")
        self.assertEqual(bridge.classify_entity_path("/crm/company/details/43/")["entity_type_id"], "4")
        with tempfile.TemporaryDirectory() as temp_dir:
            client = ExactEntityClient()
            code = bridge.command_collect_entity_context(
                client, temp_dir, "https://crm.example.test/crm/contact/details/42/", "contact"
            )
            self.assertEqual(code, 0)
            self.assertEqual(client.login_calls, 1)

    def test_project_folder_inventory_recurses_and_hashes_only_requested_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = "https://crm.example.test/docs/shared/path/root/"
            target = "https://crm.example.test/disk/downloadFile/1/?filename=proof.png"
            code = bridge.command_collect_project_folder(FolderClient(), temp_dir, root, [target], 10)
            self.assertEqual(code, 0)
            inventory = json.loads((pathlib.Path(temp_dir) / "project-folder-files.json").read_text(encoding="utf-8"))
            self.assertEqual(inventory["coverage"]["files_collected"], 2)
            proof = next(item for item in inventory["files"] if item["name"] == "proof.png")
            self.assertEqual(proof["download_status"], "DOWNLOADED")
            self.assertTrue(proof["sha256"])
            self.assertEqual(next(item for item in inventory["files"] if item["name"] == "notes.txt")["download_status"], "NOT_REQUESTED")

    def test_existing_commands_remain_registered(self):
        parser = bridge.build_parser()
        choices = next(
            action.choices
            for action in parser._actions
            if action.__class__.__name__ == "_SubParsersAction"
        )
        for command in (
            "probe",
            "fetch",
            "list-deals",
            "find-deal-by-project-number",
            "list-companies",
            "list-income-contracts",
            "download-selected-income-contract",
            "collect-company-context",
            "build-company-dossier",
            "collect-deal-context",
            "collect-entity-context",
            "collect-project-folder",
            "contract",
        ):
            self.assertIn(command, choices)


if __name__ == "__main__":
    unittest.main()
