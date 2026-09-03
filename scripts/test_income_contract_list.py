#!/usr/bin/env python3
"""Regression checks for the canonical CRM income-contract list."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import bitrix24_session_client as client_module
from bitrix24_session_client import (
    INCOME_CONTRACT_LIST_PATH,
    INCOME_CONTRACT_LIST_URL,
    SKILL_DIR,
    build_parser,
    command_collect_company_context,
    command_download_selected_income_contract,
    command_find_deal_by_project_number,
    command_list_income_contracts,
    command_record_deal_chat_resolution,
    grid_sort_url,
    paged_url,
    parse_income_contract_rows,
    validate_dossier_output_dir,
    write_failed_dossier_artifacts,
)


HTML = """
<table>
  <thead><tr>
    <th data-name="TITLE"><span class="main-grid-head-title">Название</span></th>
    <th data-name="COMPANY"><span class="main-grid-head-title">Компания</span></th>
    <th data-name="DATE" data-sort-url="/page/dogovory/dokhodnye_dogovory/?by=date&amp;order=desc"><span class="main-grid-head-title">Дата заключения</span></th>
    <th data-name="FORM"><span class="main-grid-head-title">Форма договора</span></th>
    <th data-name="NUMBER"><span class="main-grid-head-title">Номер договора</span></th>
    <th data-name="TERM"><span class="main-grid-head-title">Срок действия</span></th>
    <th data-name="FILE"><span class="main-grid-head-title">Файл договора</span></th>
  </tr></thead>
  <tbody>
    <tr class="main-grid-row main-grid-row-body" data-id="40">
      <td></td><td></td>
      <td><a href="/page/dogovory/dokhodnye_dogovory/type/142/details/40/">Договор услуг</a></td>
      <td>ООО «Тест»</td><td>26.11.2021</td><td>Услуги</td><td>Д-1</td>
      <td>до 31.12.2027 с автоматической пролонгацией</td>
      <td><a data-src="/download/40">Договор.pdf</a></td>
    </tr>
  </tbody>
</table>
"""

EMPTY_LIST_HTML = HTML.replace(
    """<tr class="main-grid-row main-grid-row-body" data-id="40">
      <td></td><td></td>
      <td><a href="/page/dogovory/dokhodnye_dogovory/type/142/details/40/">Договор услуг</a></td>
      <td>ООО «Тест»</td><td>26.11.2021</td><td>Услуги</td><td>Д-1</td>
      <td>до 31.12.2027 с автоматической пролонгацией</td>
      <td><a data-src="/download/40">Договор.pdf</a></td>
    </tr>""",
    "",
)
COMPANY_TAB_HTML = HTML.replace(
    "/page/dogovory/dokhodnye_dogovory/?by=date&amp;order=desc",
    "/company-income?by=date&amp;order=desc",
)

COMPANY_CARD_HTML = """
<h1>ООО «Тест»</h1>
<script>
tabs: [{'id':'tab_relation_dynamic_142','name':'Доходные договоры','serviceUrl':'/company-income?entityTypeId=142'}], containerId: 'crm-company-tabs'
</script>
"""
PORTAL_SHELL_HTML = """
<html><head><title>CRM portal</title></head><body><main>Portal shell</main></body></html>
"""

DEAL_SEARCH_HTML = """
<table><tbody>
  <tr class="main-grid-row main-grid-row-body" data-id="14556">
    <td><a href="/crm/deal/details/14556/">4623: Развитие цифрового сервиса</a></td>
  </tr>
</tbody></table>
"""

DEAL_SEARCH_HTML_WITH_REORDERED_CLASSES = """
<table><tbody>
  <tr data-id="14556" class="main-grid-row-body is-compact main-grid-row">
    <td><a class="crm-item" href="/crm/deal/details/14556/?IFRAME=Y">4623: Развитие цифрового сервиса</a></td>
  </tr>
</tbody></table>
"""


class FakeDealSearchClient:
    base_url = "https://crm.prof-4.ru"

    def login_portal(self):
        return "https://crm.prof-4.ru/stream/"

    def fetch(self, target):
        if str(target) != "/crm/deal/list/?FIND=4623":
            raise AssertionError(f"unexpected deal-search path: {target}")
        return "https://crm.prof-4.ru/crm/deal/list/?FIND=4623", DEAL_SEARCH_HTML


class FakeFallbackClient:
    base_url = "https://crm.prof-4.ru"

    def login_portal(self):
        return "https://crm.prof-4.ru/stream/"

    def fetch(self, target):
        value = str(target)
        if "/crm/company/details/10/" in value:
            return "https://crm.prof-4.ru/crm/company/details/10/", COMPANY_CARD_HTML
        if value.startswith("/company-income"):
            if "page=2" in value:
                return value, EMPTY_LIST_HTML
            return value, COMPANY_TAB_HTML
        if "dokhodnye_dogovory" in value:
            return value, EMPTY_LIST_HTML
        raise AssertionError(f"unexpected fetch: {value}")

    def post_form(self, target, fields):
        if not str(target).startswith("/company-income"):
            raise AssertionError(f"unexpected post: {target}")
        return "/company-income?entityTypeId=142", COMPANY_TAB_HTML


class FakeDownloadClient:
    def __init__(self):
        self.login_calls = 0

    def login_portal(self):
        self.login_calls += 1
        return "https://crm.prof-4.ru/stream/"

    def fetch_binary(self, url):
        if self.login_calls != 1:
            raise AssertionError("Скачивание PDF началось без авторизованной CRM-сессии")
        return "https://crm.prof-4.ru/download/40", b"%PDF-1.7\nselected"


class FakeIframeRequiredClient(FakeFallbackClient):
    def __init__(self):
        self.company_requests: list[str] = []

    def fetch(self, target):
        value = str(target)
        if "/crm/company/details/10/" in value:
            self.company_requests.append(value)
            if "IFRAME=Y" in value and "IFRAME_TYPE=SIDE_SLIDER" in value:
                return (
                    "https://crm.prof-4.ru/crm/company/details/10/"
                    "?IFRAME=Y&IFRAME_TYPE=SIDE_SLIDER",
                    COMPANY_CARD_HTML,
                )
            return "https://crm.prof-4.ru/crm/company/details/10/", PORTAL_SHELL_HTML
        return super().fetch(target)


class FakePackageDossierClient:
    base_url = "https://crm.prof-4.ru"

    def __init__(self, fail_company_card: bool = False):
        self.fail_company_card = fail_company_card
        self.requests: list[str] = []

    def login_portal(self):
        return "https://crm.prof-4.ru/stream/"

    def fetch(self, target):
        value = str(target)
        self.requests.append(value)
        if "/crm/company/details/10/" in value:
            if self.fail_company_card:
                raise RuntimeError("Тестовая ошибка карточки компании")
            return "https://crm.prof-4.ru/crm/company/details/10/?IFRAME=Y", COMPANY_CARD_HTML
        raise AssertionError(f"package mode requested an unrelated path: {value}")


class IncomeContractListTests(unittest.TestCase):
    def test_canonical_route_is_fixed(self) -> None:
        self.assertEqual(INCOME_CONTRACT_LIST_PATH, "/page/dogovory/dokhodnye_dogovory/")
        self.assertEqual(
            INCOME_CONTRACT_LIST_URL,
            "https://crm.prof-4.ru/page/dogovory/dokhodnye_dogovory/",
        )

    def test_parser_keeps_company_type_and_validity_from_list(self) -> None:
        rows = parse_income_contract_rows(HTML)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["company_name"], "ООО «Тест»")
        self.assertEqual(row["contract_form"], "Услуги")
        self.assertEqual(row["validity"]["status"], "PASS")
        self.assertIn("31.12.2027", row["validity"]["period_text"])
        self.assertEqual(row["validity"]["source_url"], INCOME_CONTRACT_LIST_URL)

    def test_parser_accepts_actual_contract_term_header(self) -> None:
        rows = parse_income_contract_rows(
            HTML.replace("Срок действия", "Срок договора до")
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["validity"]["status"], "PASS")
        self.assertIn("31.12.2027", rows[0]["validity"]["end_date"])
        self.assertEqual(
            rows[0]["validity"]["field_names"], ["Срок договора до"]
        )

    def test_parser_preserves_explicit_addendum_parent_contract_number(self) -> None:
        rows = parse_income_contract_rows(
            HTML.replace("Договор услуг", "Дополнительное соглашение")
            .replace("Услуги", "ДС")
            .replace("Д-1", "ДС №1 к договору №Д-1")
        )
        self.assertEqual(rows[0]["parent_contract_numbers"], ["Д-1"])

    def test_sort_and_pagination_preserve_canonical_list_path(self) -> None:
        sort_url = grid_sort_url(HTML, "Дата заключения")
        self.assertIn("/page/dogovory/dokhodnye_dogovory/", sort_url)
        self.assertIn("order=asc", sort_url)
        self.assertIn("page=2", paged_url(sort_url, 2))

    def test_dossier_can_skip_all_document_downloads(self) -> None:
        args = build_parser().parse_args([
            "build-company-dossier",
            "ООО Тест",
            "--output-dir",
            "/tmp/crm-dossier",
            "--skip-document-downloads",
        ])
        self.assertTrue(args.skip_document_downloads)

    def test_dossier_requires_explicit_output_directory(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["build-company-dossier", "ООО Тест"])

    def test_dossier_output_directory_rejects_skill_tree(self) -> None:
        with self.assertRaisesRegex(ValueError, "нельзя записывать внутри папки skills"):
            validate_dossier_output_dir("/tmp/codex/skills/bitrix24-session-bridge/bitrix24_company_contexts")

    def test_package_mode_finishes_with_required_empty_registries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakePackageDossierClient()
            with redirect_stdout(StringIO()):
                result = command_collect_company_context(
                    client,
                    "ООО Тест",
                    "10",
                    tmp,
                    1,
                    1,
                    "package",
                    True,
                )
            self.assertEqual(result, 0)
            dossier = Path(tmp) / "ооо-тест"
            self.assertEqual(json.loads((dossier / "metadata" / "run_report.json").read_text(encoding="utf-8"))["status"], "ok")
            self.assertEqual(json.loads((dossier / "metadata" / "lazy_tabs.json").read_text(encoding="utf-8")), [])
            self.assertEqual(json.loads((dossier / "metadata" / "documents.json").read_text(encoding="utf-8")), [])
            self.assertTrue((dossier / "context.md").is_file())
            self.assertFalse(any("/crm/deal/" in request for request in client.requests))

    def test_handled_company_card_failure_still_finalizes_dossier_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(StringIO()):
                result = command_collect_company_context(
                    FakePackageDossierClient(fail_company_card=True),
                    "ООО Тест",
                    "10",
                    tmp,
                    1,
                    1,
                    "package",
                    True,
                )
            self.assertEqual(result, 0)
            dossier = Path(tmp) / "ооо-тест"
            report = json.loads((dossier / "metadata" / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "partial")
            for relative_path in ("context.md", "metadata/lazy_tabs.json", "metadata/documents.json"):
                self.assertTrue((dossier / relative_path).is_file())

    def test_unhandled_failure_writes_terminal_diagnostic_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_failed_dossier_artifacts(
                tmp,
                "ООО Тест",
                "10",
                "package",
                RuntimeError("Тестовая неперехваченная ошибка"),
            )
            dossier = Path(tmp) / "ооо-тест"
            report = json.loads((dossier / "metadata" / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(len(report["errors"]), 1)
            for relative_path in ("context.md", "metadata/lazy_tabs.json", "metadata/documents.json"):
                self.assertTrue((dossier / relative_path).is_file())

    def test_company_card_tab_is_used_only_after_complete_list_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "income_contracts.json"
            with patch.object(
                client_module,
                "collect_company_matches",
                return_value=[
                    {
                        "id": "10",
                        "title": "ООО «Тест»",
                        "type": "CUSTOMER",
                        "url": "/crm/company/details/10/",
                    }
                ],
            ), redirect_stdout(StringIO()):
                result = command_list_income_contracts(
                    FakeFallbackClient(),
                    "ООО «Тест»",
                    5,
                    str(output),
                )
            self.assertEqual(result, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["source"]["mode"],
                "COMPANY_CARD_INCOME_CONTRACTS_TAB_FALLBACK",
            )
            self.assertFalse(payload["source"]["canonical_list_company_found"])
            self.assertTrue(payload["source"]["canonical_list_coverage_complete"])
            self.assertEqual(payload["source"]["company_card_url"], "https://crm.prof-4.ru/crm/company/details/10/")
            self.assertEqual(payload["source"]["tab_id"], "tab_relation_dynamic_142")
            self.assertEqual(payload["source"]["tab_name"], "Доходные договоры")
            self.assertEqual(payload["rows"][0]["contract_form"], "Услуги")
            self.assertEqual(payload["rows"][0]["source_mode"], payload["source"]["mode"])

    def test_plain_company_shell_is_retried_as_iframe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "income_contracts.json"
            client = FakeIframeRequiredClient()
            with redirect_stdout(StringIO()):
                result = command_list_income_contracts(
                    client,
                    "ООО «Тест»",
                    5,
                    str(output),
                    "10",
                )
            self.assertEqual(result, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                client.company_requests,
                [
                    "/crm/company/details/10/",
                    "/crm/company/details/10/?IFRAME=Y&IFRAME_TYPE=SIDE_SLIDER",
                ],
            )
            self.assertEqual(
                payload["source"]["company_card_url"],
                "https://crm.prof-4.ru/crm/company/details/10/"
                "?IFRAME=Y&IFRAME_TYPE=SIDE_SLIDER",
            )
            self.assertEqual(len(payload["source"]["company_card_attempted_urls"]), 2)
            self.assertEqual(payload["rows"][0]["contract_form"], "Услуги")

    def test_saved_iframe_url_has_priority_and_must_match_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "income_contracts.json"
            client = FakeIframeRequiredClient()
            saved_url = (
                "https://crm.prof-4.ru/crm/company/details/10/"
                "?IFRAME=Y&IFRAME_TYPE=SIDE_SLIDER"
            )
            with redirect_stdout(StringIO()):
                result = command_list_income_contracts(
                    client,
                    "ООО «Тест»",
                    5,
                    str(output),
                    "10",
                    saved_url,
                )
            self.assertEqual(result, 0)
            self.assertEqual(client.company_requests, [saved_url])
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"]["company_card_url"], saved_url)

            with self.assertRaisesRegex(ValueError, "exact --company-id card"):
                command_list_income_contracts(
                    FakeIframeRequiredClient(),
                    "ООО «Тест»",
                    5,
                    str(output),
                    "10",
                    "https://crm.prof-4.ru/crm/company/details/11/",
                )

    def test_downloads_only_file_from_passed_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection = root / "selection.json"
            selection.write_text(json.dumps({
                "status": "PASS",
                "selected": {
                    "id": "40",
                    "detail_url": "/page/dogovory/dokhodnye_dogovory/type/142/details/40/",
                    "contract_file_urls": ["/download/40"],
                },
            }, ensure_ascii=False), encoding="utf-8")
            output = root / "download"
            client = FakeDownloadClient()
            with redirect_stdout(StringIO()):
                result = command_download_selected_income_contract(
                    client,
                    str(selection),
                    str(output),
                )
            self.assertEqual(result, 0)
            self.assertEqual(client.login_calls, 1)
            manifest = json.loads((output / "income-contract-download.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["download_scope"], "SELECTED_INCOME_CONTRACT_ONLY")
            self.assertEqual(manifest["selected_contract_id"], "40")
            self.assertTrue(Path(manifest["file"]).is_file())

    def test_exact_project_lookup_uses_native_find_and_title_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "deal-project-search.json"
            with redirect_stdout(StringIO()):
                result = command_find_deal_by_project_number(
                    FakeDealSearchClient(), "4623", str(output)
                )
            self.assertEqual(result, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["selected_deal"]["deal_id"], "14556")
            self.assertEqual(
                payload["selected_deal"]["deal_url"],
                "https://crm.prof-4.ru/crm/deal/details/14556/",
            )

    def test_exact_project_lookup_accepts_crm_row_when_css_classes_are_reordered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "deal-project-search.json"
            client = FakeDealSearchClient()
            with patch.object(client, "fetch", return_value=(
                "https://crm.prof-4.ru/crm/deal/list/?FIND=4623",
                DEAL_SEARCH_HTML_WITH_REORDERED_CLASSES,
            )), redirect_stdout(StringIO()):
                result = command_find_deal_by_project_number(client, "4623", str(output))
            self.assertEqual(result, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["selected_deal"]["deal_url"],
                "https://crm.prof-4.ru/crm/deal/details/14556/?IFRAME=Y",
            )

    def test_exact_project_lookup_blocks_non_four_digit_input_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "четырёх цифр"):
                command_find_deal_by_project_number(
                    FakeDealSearchClient(), "462", str(Path(tmp) / "report.json")
                )

    def test_interactive_chat_resolution_creates_master_ready_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            search_path = root / "deal-project-search.json"
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    command_find_deal_by_project_number(
                        FakeDealSearchClient(), "4623", str(search_path)
                    ),
                    0,
                )
            output = root / "tax-status-chat-resolution.json"
            with redirect_stdout(StringIO()):
                result = command_record_deal_chat_resolution(
                    str(search_path),
                    "https://crm.prof-4.ru/online/?IM_DIALOG=chat4623",
                    "Сделка: 4623: Развитие цифрового сервиса",
                    "https://crm.prof-4.ru/crm/deal/details/14556/?IFRAME=Y",
                    "SMZ",
                    ["chat:4623/message:71"],
                    "Финальный статус исполнителя — СМЗ.",
                    "2026-08-25",
                    str(output),
                )
            self.assertEqual(result, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "RESOLVED")
            self.assertEqual(payload["resolved_tax_status"], "SMZ")
            self.assertTrue(payload["chat_header_verified"])
            self.assertTrue(payload["chat_deal_url_verified"])
            self.assertEqual(payload["verification_method"], "INTERACTIVE_BROWSER_SESSION")

    def test_interactive_chat_resolution_rejects_link_to_another_deal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            search_path = root / "deal-project-search.json"
            with redirect_stdout(StringIO()):
                command_find_deal_by_project_number(FakeDealSearchClient(), "4623", str(search_path))
            output = root / "tax-status-chat-resolution.json"
            with redirect_stdout(StringIO()):
                result = command_record_deal_chat_resolution(
                    str(search_path),
                    "https://crm.prof-4.ru/online/?IM_DIALOG=chat4623",
                    "Сделка: 4623: Развитие цифрового сервиса",
                    "https://crm.prof-4.ru/crm/deal/details/99999/",
                    "FL",
                    ["chat:4623/message:72"],
                    "Финальный статус исполнителя — ФЛ.",
                    "2026-08-25",
                    str(output),
                )
            self.assertEqual(result, 2)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "UNRESOLVED")
            self.assertIn("CRM_DEAL_CHAT_LINK_MISMATCH", payload["errors"])


if __name__ == "__main__":
    unittest.main()
