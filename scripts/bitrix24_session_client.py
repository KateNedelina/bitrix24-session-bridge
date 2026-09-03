#!/usr/bin/env python3
"""Сессионный клиент для Bitrix24.

Выполняет вход через стандартную веб-форму Bitrix24, хранит cookies
и собирает CRM-контекст по компании, включая карточки, историю, документы
и архивные снимки страниц.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import html
import http.client
import http.cookiejar
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import shutil
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Iterable


SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
ENV_PATH = SKILL_DIR / ".env"
BRIDGE_VERSION = "0.3.0-candidate"
BRIDGE_CONTRACT_VERSION = "1.1"
BRIDGE_CAPABILITIES = (
    "deal_outer_and_side_slider_fetch",
    "exact_deal_model_selection",
    "empty_registered_fields",
    "field_schema_export",
    "generic_exact_entity_collection",
    "contact_and_company_entity_collection",
    "linked_entity_references",
    "standard_field_schema",
    "reference_display_value_resolution",
    "project_folder_inventory",
    "read_only_collection",
)
COLLECT_MODES = ("quick", "package", "full", "deep")
DEFAULT_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_DOCUMENT_DOWNLOADS = 120
DEFAULT_MAX_RELATED_CARDS = 120
INCOME_CONTRACT_LIST_PATH = "/page/dogovory/dokhodnye_dogovory/"
INCOME_CONTRACT_LIST_URL = "https://crm.prof-4.ru/page/dogovory/dokhodnye_dogovory/"
INCOME_CONTRACT_CHAIN_SELECTION_HINT = "ACTIVE_BASE_CONTRACT_CHAIN_COVERING_PROJECT_PERIOD"
GENERATED_METADATA_FILES = (
    "communications.tsv",
    "company_details.json",
    "deal_details.json",
    "deal_matches.json",
    "document_entrypoints.json",
    "documents.json",
    "entity_links.json",
    "income_contracts.json",
    "lazy_tabs.json",
    "related_entities.json",
    "run_report.json",
    "tabs.json",
    "timeline_highlights.json",
)
CORE_DOSSIER_FILES = (
    "context.md",
    "metadata/run_report.json",
    "metadata/lazy_tabs.json",
    "metadata/documents.json",
)
CRM_ENTITY_TYPES = {
    "1": "lead",
    "2": "deal",
    "3": "contact",
    "4": "company",
    "7": "quote",
    "31": "smart_invoice",
}

# Standard fields are part of the stable Bitrix CRM model even where the card's
# embedded editor config exposes only user fields.  Keeping this map generic
# lets a consumer prove the code/title/type of a standard field instead of
# treating a missing editor fragment as a value-type inference.  A title read
# from the actual page always overrides this fallback metadata.
STANDARD_FIELD_SCHEMA: dict[str, dict[str, object]] = {
    "ID": {"field_title": "ID", "field_type": "integer", "multiple": False},
    "TITLE": {"field_title": "Название", "field_type": "string", "multiple": False},
    "ASSIGNED_BY_ID": {"field_title": "Ответственный", "field_type": "user", "multiple": False},
    "ASSIGNED_BY_FORMATTED_NAME": {"field_title": "Ответственный", "field_type": "string", "multiple": False},
    "COMPANY_ID": {"field_title": "Компания", "field_type": "crm_reference", "multiple": False},
    "CONTACT_ID": {"field_title": "Контакт", "field_type": "crm_reference", "multiple": False},
    "CONTACT_IDS": {"field_title": "Контакты", "field_type": "crm_reference", "multiple": True},
    "COMPANY_TITLE": {"field_title": "Компания", "field_type": "string", "multiple": False},
    "CONTACT_FULL_NAME": {"field_title": "Контакт", "field_type": "string", "multiple": False},
    "LAST_NAME": {"field_title": "Фамилия", "field_type": "string", "multiple": False},
    "NAME": {"field_title": "Имя", "field_type": "string", "multiple": False},
    "SECOND_NAME": {"field_title": "Отчество", "field_type": "string", "multiple": False},
    "POST": {"field_title": "Должность", "field_type": "string", "multiple": False},
    "EMAIL": {"field_title": "E-mail", "field_type": "multifield", "multiple": True},
    "PHONE": {"field_title": "Телефон", "field_type": "multifield", "multiple": True},
    "DATE_CREATE": {"field_title": "Дата создания", "field_type": "datetime", "multiple": False},
    "DATE_MODIFY": {"field_title": "Дата изменения", "field_type": "datetime", "multiple": False},
}
COMPANY_TAB_COLLECT_DENYLIST = {
    "crm_rest_marketplace",
}


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана обязательная переменная окружения: {name}")
    return value


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def load_env_file(path: pathlib.Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def to_absolute(base_url: str, url_or_path: str) -> str:
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        candidate = url_or_path
    else:
        candidate = url_or_path if url_or_path.startswith("/") else "/" + url_or_path
        candidate = base_url + candidate
    parsed = urllib.parse.urlsplit(candidate)
    # Bitrix can expose a human-readable shared-disk path in inline scripts.
    # urllib.request requires its path to be percent-encoded on the wire.
    path = urllib.parse.quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def clean_text(raw_html: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>", " ", raw_html)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_bbcode(value: str) -> str:
    value = re.sub(r"\[url=([^\]]+)](.*?)\[/url]", r"\2 (\1)", value, flags=re.I | re.S)
    value = re.sub(r"\[(?:/?[a-z]+|[a-z]+=[^\]]+)]", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def extract_bxajaxid(raw_html: str) -> str:
    match = re.search(r"bxajaxid=([a-f0-9]+)", raw_html)
    if not match:
        raise RuntimeError("Не удалось найти bxajaxid в HTML списка сделок")
    return match.group(1)


def extract_sessid(raw_html: str) -> str | None:
    match = re.search(r'name="sessid"[^>]*value="([^"]+)"', raw_html)
    return match.group(1) if match else None


def strip_tags(raw_html: str) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-") or "company"


def parse_deal_rows(raw_html: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    row_re = re.compile(
        r'<tr\b(?=[^>]*\bdata-id="(?P<id>\d+)")(?=[^>]*\bclass="[^"]*\bmain-grid-row\b)[^>]*>.*?</tr>',
        re.S,
    )
    for match in row_re.finditer(raw_html):
        row_html = match.group(0)
        deal_id = match.group("id")
        detail_match = re.search(
            r'href="(?P<url>/crm/deal/details/\d+/[^\"]*)"[^>]*>(?P<title>.*?)</a>',
            row_html,
            re.S,
        )
        desc_matches = re.findall(r'<div class="crm-info-description-wrapper">(.*?)</div>', row_html, re.S)
        owner_matches = re.findall(r'<a href="/company/personal/user/\d+/".*?>(.*?)</a>', row_html, re.S)
        stage_matches = re.findall(
            r'<td class="main-grid-cell main-grid-cell-left"[^>]*><div class="main-grid-cell-inner"><span class="main-grid-cell-content"[^>]*>(.*?)</span>',
            row_html,
            re.S,
        )
        title = strip_tags(detail_match.group("title")) if detail_match else ""
        company = strip_tags(desc_matches[1]) if len(desc_matches) > 1 else ""
        responsible = strip_tags(owner_matches[-1]) if owner_matches else ""
        stage = strip_tags(stage_matches[2]) if len(stage_matches) >= 3 else ""
        rows.append(
            {
                "id": deal_id,
                "title": title,
                "company": company,
                "stage": stage,
                "responsible": responsible,
                "amount": "",
                "date_create": "",
                "contact": "",
                "url": detail_match.group("url") if detail_match else f"/crm/deal/details/{deal_id}/",
            }
        )
    return rows


def normalize_four_digit_project_number(value: str) -> str:
    """Return the CRM-searchable project number or fail before a broad search."""
    number = str(value).strip()
    if not re.fullmatch(r"\d{4}", number):
        raise ValueError("Номер проекта для поиска чата сделки должен состоять ровно из четырёх цифр")
    return number


def normalized_crm_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def canonical_deal_url(value: object) -> str | None:
    """Return a query-free URL identity for one CRM deal card.

    The chat's ``ОТКРЫТЬ СДЕЛКУ`` link can legitimately add IFRAME parameters,
    so equality is established by the exact deal id rather than by the raw URL.
    """
    raw = str(value or "").strip()
    match = re.search(r"/crm/deal/details/(\d+)/", raw)
    if not match:
        return None
    parsed = urllib.parse.urlparse(raw)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    return f"{origin}/crm/deal/details/{match.group(1)}/"


def read_json_object(path: str) -> dict[str, object]:
    candidate = pathlib.Path(path)
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать JSON-доказательство: {candidate}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON-доказательство должно быть объектом: {candidate}")
    return payload


def command_find_deal_by_project_number(
    client: "BitrixSessionClient", project_number: str, output: str
) -> int:
    """Resolve one deal by its exact four-digit project prefix, read-only.

    This deliberately searches the CRM deal list through its native FIND query,
    then accepts only titles beginning with ``NNNN:``.  A chat is not inferred
    here: the master must open the selected card and verify the chat header.
    """
    number = normalize_four_digit_project_number(project_number)
    target = "/crm/deal/list/?" + urllib.parse.urlencode({"FIND": number})
    client.login_portal()
    final_url, raw_html = client.fetch(target)
    pattern = re.compile(rf"^\s*{re.escape(number)}\s*:")
    matches = [row for row in parse_deal_rows(raw_html) if pattern.match(row["title"])]
    by_id = {str(row["id"]): row for row in matches}
    exact = list(by_id.values())

    report: dict[str, object] = {
        "schema_version": "1.0",
        "operation": "EXACT_DEAL_LOOKUP_FOR_CRM_CHAT",
        "project_number": number,
        "search_url": final_url,
        "match_rule": f"deal title begins with {number}:",
        "status": "PASS" if len(exact) == 1 else "BLOCKED",
        "candidates": [
            {
                "deal_id": row["id"],
                "title": row["title"],
                "deal_url": to_absolute(client.base_url, row["url"]),
            }
            for row in exact
        ],
    }
    if len(exact) == 1:
        row = exact[0]
        report["selected_deal"] = {
            "deal_id": row["id"],
            "title": row["title"],
            "deal_url": to_absolute(client.base_url, row["url"]),
        }
    elif not exact:
        report["blocker"] = "DEAL_PROJECT_NUMBER_NOT_FOUND"
    else:
        report["blocker"] = "DEAL_PROJECT_NUMBER_NOT_UNIQUE"

    output_path = pathlib.Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_file(output_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 2


def command_record_deal_chat_resolution(
    deal_search_report: str,
    chat_url: str,
    chat_header: str,
    open_deal_url: str,
    resolved_tax_status: str,
    message_locators: list[str],
    message_text: str,
    reviewed_at: str,
    output: str,
) -> int:
    """Create one validated handoff from an interactive CRM chat review.

    Messages in this portal are loaded dynamically.  The browser-capable agent
    reads the visible chat and passes only the exact, reviewable facts here;
    this command neither falls back to an HTTP snapshot nor guesses a status.
    """
    search = read_json_object(deal_search_report)
    selected = search.get("selected_deal")
    errors: list[str] = []
    if search.get("status") != "PASS" or not isinstance(selected, dict):
        errors.append("DEAL_SEARCH_REPORT_NOT_UNAMBIGUOUS")
        selected = {}

    project_number = normalize_four_digit_project_number(str(search.get("project_number", "")))
    selected_title = normalized_crm_text(selected.get("title"))
    selected_url = str(selected.get("deal_url", ""))
    selected_canonical = canonical_deal_url(selected_url)
    selected_deal_id = str(selected.get("deal_id", "")).strip()
    expected_header = normalized_crm_text(f"Сделка: {selected_title}")
    if not selected_title or normalized_crm_text(chat_header) != expected_header:
        errors.append("CRM_DEAL_CHAT_HEADER_MISMATCH")

    opened_canonical = canonical_deal_url(open_deal_url)
    if not selected_canonical or opened_canonical != selected_canonical:
        errors.append("CRM_DEAL_CHAT_LINK_MISMATCH")

    status = str(resolved_tax_status or "").strip().upper()
    if status not in {"SMZ", "IP", "FL"}:
        errors.append("CRM_DEAL_CHAT_FINAL_STATUS_INVALID")
    locators = [normalized_crm_text(item) for item in message_locators if normalized_crm_text(item)]
    if not locators or not normalized_crm_text(message_text):
        errors.append("CRM_DEAL_CHAT_FINAL_ASSIGNMENT_NOT_EXPLICIT")
    try:
        dt.date.fromisoformat(str(reviewed_at))
    except ValueError:
        errors.append("CRM_DEAL_CHAT_REVIEW_DATE_INVALID")
    if not str(chat_url or "").strip():
        errors.append("CRM_DEAL_CHAT_NOT_OPENED")

    report: dict[str, object] = {
        "schema_version": "1.1",
        "operation": "INTERACTIVE_CRM_DEAL_CHAT_TAX_STATUS_RESOLUTION",
        "verification_method": "INTERACTIVE_BROWSER_SESSION",
        "status": "RESOLVED" if not errors else "UNRESOLVED",
        "resolved_tax_status": status if status in {"SMZ", "IP", "FL"} else None,
        "final_assignment_explicit": bool(locators and normalized_crm_text(message_text)),
        "deal_match_unambiguous": search.get("status") == "PASS" and bool(selected_deal_id),
        "deal_id": selected_deal_id or None,
        "deal_url": selected_url or None,
        "project_number": project_number,
        "deal_search_report": deal_search_report,
        "chat_url": str(chat_url or "").strip() or None,
        "chat_header": normalized_crm_text(chat_header),
        "chat_header_verified": "CRM_DEAL_CHAT_HEADER_MISMATCH" not in errors,
        "open_deal_url": str(open_deal_url or "").strip() or None,
        "chat_deal_url_verified": "CRM_DEAL_CHAT_LINK_MISMATCH" not in errors,
        "message_locators": locators,
        "message_text_sha256": hashlib.sha256(normalized_crm_text(message_text).encode("utf-8")).hexdigest()
        if normalized_crm_text(message_text) else None,
        "reviewed_at": str(reviewed_at),
        "resolution_reason": "EXPLICIT_STATUS_ASSIGNMENT" if not errors else None,
        "errors": errors,
    }
    output_path = pathlib.Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_file(output_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


class MainGridRowParser(HTMLParser):
    """Extract top-level cells from Bitrix main-grid body rows."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, object]] = []
        self.active = False
        self.tr_depth = 0
        self.cell_depth = 0
        self.row_id = ""
        self.cells: list[dict[str, object]] = []
        self.current_cell: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "tr":
            classes = set(attrs_dict.get("class", "").split())
            if not self.active and {"main-grid-row", "main-grid-row-body"}.issubset(classes):
                self.active = True
                self.tr_depth = 1
                self.row_id = attrs_dict.get("data-id", "")
                self.cells = []
                return
            if self.active:
                self.tr_depth += 1
        if not self.active:
            return
        if tag == "td":
            if self.current_cell is None and self.tr_depth == 1:
                self.current_cell = {"text": [], "links": [], "data_srcs": []}
                self.cell_depth = 1
            elif self.current_cell is not None:
                self.cell_depth += 1
            return
        if self.current_cell is not None and tag == "a":
            href = attrs_dict.get("href", "")
            data_src = attrs_dict.get("data-src", "")
            if href:
                links = self.current_cell["links"]
                assert isinstance(links, list)
                links.append(html.unescape(href))
            if data_src:
                sources = self.current_cell["data_srcs"]
                assert isinstance(sources, list)
                sources.append(html.unescape(data_src))

    def handle_endtag(self, tag: str) -> None:
        if not self.active:
            return
        if tag == "td" and self.current_cell is not None:
            self.cell_depth -= 1
            if self.cell_depth == 0:
                chunks = self.current_cell["text"]
                assert isinstance(chunks, list)
                self.current_cell["text"] = re.sub(r"\s+", " ", " ".join(chunks)).strip()
                self.cells.append(self.current_cell)
                self.current_cell = None
            return
        if tag == "tr":
            self.tr_depth -= 1
            if self.tr_depth == 0:
                self.rows.append({"id": self.row_id, "cells": self.cells})
                self.active = False

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None and data.strip():
            chunks = self.current_cell["text"]
            assert isinstance(chunks, list)
            chunks.append(data.strip())


def main_grid_headers(raw_html: str) -> list[dict[str, str]]:
    headers: list[dict[str, str]] = []
    for match in re.finditer(r"<th\b(?P<attrs>[^>]*)>(?P<body>.*?)</th>", raw_html, re.S | re.I):
        name_match = re.search(r'data-name="([^"]+)"', match.group("attrs"), re.I)
        title_match = re.search(
            r'<span[^>]*class="[^"]*main-grid-head-title[^"]*"[^>]*>(.*?)</span>',
            match.group("body"),
            re.S | re.I,
        )
        if name_match and title_match:
            headers.append({"name": name_match.group(1), "title": strip_tags(title_match.group(1))})
    return headers


def grid_sort_url(raw_html: str, header_title: str) -> str:
    expected = re.sub(r"\s+", " ", header_title).strip().casefold()
    for match in re.finditer(r"<th\b(?P<attrs>[^>]*)>(?P<body>.*?)</th>", raw_html, re.S | re.I):
        title_match = re.search(
            r'<span[^>]*class="[^"]*main-grid-head-title[^"]*"[^>]*>(.*?)</span>',
            match.group("body"),
            re.S | re.I,
        )
        if not title_match or strip_tags(title_match.group(1)).casefold() != expected:
            continue
        url_match = re.search(r'data-sort-url="([^"]+)"', match.group("attrs"), re.I)
        if not url_match:
            return ""
        raw_url = html.unescape(url_match.group(1))
        parsed = urllib.parse.urlsplit(raw_url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key.casefold() != "order"]
        query.append(("order", "asc"))
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
        )
    return ""


def parse_income_contract_rows(raw_html: str) -> list[dict[str, object]]:
    """Parse the canonical CRM income-contract grid (dynamic type 142)."""
    headers = main_grid_headers(raw_html)
    parser = MainGridRowParser()
    parser.feed(raw_html)
    rows: list[dict[str, object]] = []
    for parsed in parser.rows:
        cells = parsed.get("cells")
        if not isinstance(cells, list) or len(cells) < len(headers) + 2:
            continue
        mapped = {
            header["title"]: cells[index + 2]
            for index, header in enumerate(headers)
        }
        row_id = str(parsed.get("id") or "")
        if not row_id.isdigit():
            continue
        title_cell = mapped.get("Название") or {}
        file_cell = mapped.get("Файл договора") or {}
        title_links = title_cell.get("links") if isinstance(title_cell, dict) else []
        file_links = file_cell.get("data_srcs") if isinstance(file_cell, dict) else []
        detail_url = next(
            (str(link) for link in (title_links or []) if f"/details/{row_id}/" in str(link)),
            "",
        )
        text_fields = {
            title: str(cell.get("text") or "")
            for title, cell in mapped.items()
            if isinstance(cell, dict)
        }

        def first_field(*titles: str) -> tuple[str, str]:
            normalized_fields = {
                re.sub(r"\s+", " ", key).strip().casefold(): (key, value)
                for key, value in text_fields.items()
            }
            for title in titles:
                match = normalized_fields.get(title.casefold())
                if match and match[1].strip():
                    return match
            return "", ""

        company_field, company_name = first_field(
            "Компания", "Клиент", "Заказчик", "ДО", "Юридическое лицо"
        )
        period_field, validity_period = first_field("Срок действия", "Период действия")
        start_field, validity_start = first_field(
            "Дата начала действия", "Начало действия", "Действует с", "Дата начала"
        )
        end_field, validity_end = first_field(
            "Дата окончания действия",
            "Окончание действия",
            "Действует до",
            "Дата окончания",
            "Срок договора до",
        )
        validity_fields = [
            field for field in (period_field, start_field, end_field) if field
        ]
        relation_text = "\n".join(
            [
                str(title_cell.get("text") or "") if isinstance(title_cell, dict) else "",
                str((mapped.get("Номер договора") or {}).get("text") or ""),
                *(f"{key}: {value}" for key, value in text_fields.items()),
            ]
        )
        parent_numbers = []
        for pattern in (
            r"\b(?:к|по)\s+(?:рамочн\w*\s+)?договор\w*\s*(?:№|N)?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9_./-]*)",
            r"\bдс\s*№?\s*[A-Za-zА-Яа-яЁё0-9_./-]*\s+к\s+(?!договор\w*\b)(?:№|N)?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9_./-]*)",
        ):
            parent_numbers.extend(match.group(1) for match in re.finditer(pattern, relation_text, re.IGNORECASE))
        rows.append(
            {
                "id": row_id,
                "title": str(title_cell.get("text") or "") if isinstance(title_cell, dict) else "",
                "conclusion_date": str((mapped.get("Дата заключения") or {}).get("text") or ""),
                "contract_form": str((mapped.get("Форма договора") or {}).get("text") or ""),
                "contract_number": str((mapped.get("Номер договора") or {}).get("text") or ""),
                "detail_url": detail_url,
                "contract_file_label": str(file_cell.get("text") or "") if isinstance(file_cell, dict) else "",
                "contract_file_urls": list(dict.fromkeys(str(item) for item in (file_links or []) if item)),
                "company_name": company_name,
                "company_field": company_field,
                "fields": text_fields,
                "parent_contract_numbers": list(dict.fromkeys(parent_numbers)),
                "validity": {
                    "status": "PASS" if validity_period or validity_end else "MISSING",
                    "period_text": validity_period,
                    "start_date": validity_start,
                    "end_date": validity_end,
                    "field_names": validity_fields,
                    "source_url": INCOME_CONTRACT_LIST_URL,
                    "row_id": row_id,
                },
            }
        )
    return rows


def paged_url(raw_url: str, page: int) -> str:
    parsed = urllib.parse.urlsplit(raw_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key.casefold() != "page"]
    query.append(("page", str(page)))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def normalize_crm_path(raw_url: str) -> str:
    value = js_unescape(html.unescape(raw_url)).strip()
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value)
    path = parsed.path if parsed.scheme and parsed.netloc else value.split("?", 1)[0].split("#", 1)[0]
    if parsed.scheme and parsed.netloc:
        query = parsed.query
    else:
        query = urllib.parse.urlparse(value).query
    if not path.startswith("/"):
        path = "/" + path
    if "/details/" in path:
        path = re.sub(r"(/details/\d+)/.*$", r"\1/", path)
    return urllib.parse.urlunparse(("", "", path, "", query, "")) if query and "/details/" not in path else path


def classify_entity_path(path: str) -> dict[str, str] | None:
    path = normalize_crm_path(path)
    standard_match = re.search(r"/crm/(?P<kind>contact|company)/details/(?P<id>\d+)/", path)
    if standard_match:
        kind = standard_match.group("kind")
        item_id = standard_match.group("id")
        if item_id == "0":
            return None
        entity_type_id = "3" if kind == "contact" else "4"
        return {
            "kind": kind,
            "entity_type_id": entity_type_id,
            "id": item_id,
            "url": f"/crm/{kind}/details/{item_id}/",
        }
    deal_match = re.search(r"/crm/deal/details/(?P<id>\d+)/", path)
    if deal_match:
        deal_id = deal_match.group("id")
        if deal_id == "0":
            return None
        return {
            "kind": "deal",
            "entity_type_id": "2",
            "id": deal_id,
            "url": f"/crm/deal/details/{deal_id}/",
        }
    dynamic_match = re.search(r"/crm/type/(?P<type>\d+)/details/(?P<id>\d+)/", path)
    if dynamic_match:
        entity_type_id = dynamic_match.group("type")
        item_id = dynamic_match.group("id")
        if item_id == "0":
            return None
        return {
            "kind": CRM_ENTITY_TYPES.get(entity_type_id, "dynamic"),
            "entity_type_id": entity_type_id,
            "id": item_id,
            "url": f"/crm/type/{entity_type_id}/details/{item_id}/",
        }
    page_dynamic_match = re.search(r"/page/[^\"'\s<>]+/type/(?P<type>\d+)/details/(?P<id>\d+)/", path)
    if page_dynamic_match:
        entity_type_id = page_dynamic_match.group("type")
        item_id = page_dynamic_match.group("id")
        if item_id == "0":
            return None
        normalized = re.sub(r"(/details/\d+)/.*$", r"\1/", path)
        return {
            "kind": CRM_ENTITY_TYPES.get(entity_type_id, "dynamic"),
            "entity_type_id": entity_type_id,
            "id": item_id,
            "url": normalized,
        }
    return None


def merge_entity_ref(refs: list[dict[str, str]], ref: dict[str, str]) -> None:
    key = (ref.get("entity_type_id", ""), ref.get("id", ""), ref.get("url", ""))
    if not key[1] and not key[2]:
        return
    for existing in refs:
        existing_key = (existing.get("entity_type_id", ""), existing.get("id", ""), existing.get("url", ""))
        if existing_key != key:
            continue
        for field, value in ref.items():
            if value and not existing.get(field):
                existing[field] = value
        return
    refs.append(ref)


def extract_entity_refs(raw_html: str, source: str = "") -> list[dict[str, str]]:
    title_by_url: dict[str, str] = {}
    anchor_re = re.compile(r'<a\b[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S | re.I)
    for match in anchor_re.finditer(raw_html):
        path = normalize_crm_path(match.group("href"))
        title = strip_tags(match.group("title"))
        if path and title:
            title_by_url.setdefault(path, title)

    # Searching from every `text` field to a later redirect action across the
    # whole CRM page causes catastrophic regex backtracking on large cards.
    # Match redirect actions linearly, then look for the closest title only in
    # a bounded prefix of the surrounding serialized block.
    redirect_re = re.compile(
        r'"action":\{"type":"redirect","value":"(?P<url>(?:\\.|[^"])*)"\}'
    )
    title_re = re.compile(r'"text":"(?P<title>(?:\\.|[^"])*)"')
    for match in redirect_re.finditer(raw_html):
        path = normalize_crm_path(match.group("url"))
        prefix = raw_html[max(0, match.start() - 4000) : match.start()]
        title_matches = list(title_re.finditer(prefix))
        title = js_unescape(title_matches[-1].group("title")) if title_matches else ""
        if path and title:
            title_by_url.setdefault(path, title)

    candidates: list[str] = []
    candidates.extend(re.findall(r'href="([^"]+)"', raw_html, re.I))
    candidates.extend(extract_redirect_links(raw_html))
    candidates.extend(re.findall(r'"value":"((?:\\.|[^"])*(?:/crm/deal/details/|/crm/type/|/page/)[^"]*)"', raw_html))
    candidates.extend(re.findall(r'"show":"((?:\\.|[^"])*(?:/crm/deal/details/|/crm/type/|/page/)[^"]*)"', raw_html))
    candidates.extend(re.findall(r"'SHOW_URL':'([^']+)'", raw_html))

    refs: list[dict[str, str]] = []
    for candidate in candidates:
        path = normalize_crm_path(candidate)
        classified = classify_entity_path(path)
        if not classified:
            continue
        classified["title"] = title_by_url.get(classified["url"], title_by_url.get(path, ""))
        classified["source"] = source
        merge_entity_ref(refs, classified)
    return refs


def extract_balanced_object(text: str, start: int) -> tuple[str, int] | None:
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1], index + 1
    return None


def extract_entity_data_objects(raw_html: str) -> list[dict[str, object]]:
    objects: list[dict[str, object]] = []
    pattern = re.compile(r"\{\s*entityTypeId:\s*(?P<type>\d+),\s*data:\s*", re.S)
    for match in pattern.finditer(raw_html):
        start = raw_html.find("{", match.end())
        extracted = extract_balanced_object(raw_html, start)
        if not extracted:
            continue
        raw_json, _ = extracted
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            objects.append({"entity_type_id": match.group("type"), "data": data})
    return objects


def infer_field_type(value: object) -> tuple[str, bool]:
    """Return a conservative transport type when Bitrix did not expose schema."""
    if isinstance(value, list):
        return "multiple", True
    if isinstance(value, bool):
        return "boolean", False
    if isinstance(value, int):
        return "integer", False
    if isinstance(value, float):
        return "decimal", False
    if isinstance(value, dict):
        if "VALUE" in value:
            return infer_field_type(value.get("VALUE"))
        return "crm_reference", False
    return "string", False


def extract_field_schema(raw_html: str, raw_fields: dict[str, object]) -> dict[str, dict[str, object]]:
    """Extract complete, evidence-bearing schema fragments from one card.

    Bitrix installations serialize field configuration in several slightly
    different JavaScript shapes.  This parser intentionally accepts only an
    explicit field code followed by an object; unobserved metadata remains null.
    """
    schema: dict[str, dict[str, object]] = {}

    def ensure_record(code: str, value: object | None = None) -> dict[str, object]:
        existing = schema.get(code)
        if existing is not None:
            return existing
        inferred_type, inferred_multiple = infer_field_type(value)
        standard = STANDARD_FIELD_SCHEMA.get(code)
        record: dict[str, object] = {
            "field_code": code,
            "field_title": standard.get("field_title") if standard else None,
            "field_type": standard.get("field_type") if standard else inferred_type,
            "multiple": standard.get("multiple") if standard else inferred_multiple,
            "settings": {},
            "metadata_source": "STANDARD_BITRIX_FIELD_SCHEMA" if standard else "VALUE_TYPE_INFERENCE",
        }
        schema[code] = record
        return record

    for code, value in raw_fields.items():
        ensure_record(code, value)

    def balanced_array(start: int) -> str | None:
        depth = 0
        quote: str | None = None
        escaped = False
        for index in range(start, len(raw_html)):
            char = raw_html[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return raw_html[start:index + 1]
        return None

    def walk(value: object) -> Iterable[dict[str, object]]:
        if isinstance(value, dict):
            if isinstance(value.get("name"), str) and isinstance(value.get("data"), dict):
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    # The entity editor's ``current`` config is Python-literal-compatible after
    # replacing JavaScript primitives. It carries the real title/type/list data.
    for current_match in re.finditer(r"\bcurrent\s*:\s*\[", raw_html):
        array_start = raw_html.find("[", current_match.start())
        array_text = balanced_array(array_start)
        if not array_text:
            continue
        literal = re.sub(r"\btrue\b", "True", array_text)
        literal = re.sub(r"\bfalse\b", "False", literal)
        literal = re.sub(r"\bnull\b", "None", literal)
        try:
            current_config = ast.literal_eval(literal)
        except (SyntaxError, ValueError):
            continue
        for definition in walk(current_config):
            code = str(definition.get("name"))
            record = ensure_record(code, raw_fields.get(code))
            data = definition.get("data") if isinstance(definition.get("data"), dict) else {}
            info = data.get("fieldInfo") if isinstance(data.get("fieldInfo"), dict) else {}
            record["field_title"] = str(definition.get("title") or "").strip() or record.get("field_title")
            record["field_type"] = str(info.get("USER_TYPE_ID") or definition.get("type") or record.get("field_type"))
            multiple = info.get("MULTIPLE")
            if multiple is not None:
                record["multiple"] = str(multiple).upper() in {"Y", "TRUE", "1"}
            record["settings"] = dict(info)
            enum_rows = info.get("ENUM") or info.get("ITEMS") or info.get("OPTIONS")
            if isinstance(enum_rows, list):
                options = {str(item.get("ID")): str(item.get("VALUE") or "") for item in enum_rows
                           if isinstance(item, dict) and item.get("ID") is not None}
                if options:
                    record["enumeration_options"] = options
                    record["display_options"] = options
                    record["option_list_version"] = "sha256:" + hashlib.sha256(
                        json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
            record["metadata_source"] = "BITRIX_ENTITY_EDITOR_CURRENT_CONFIG"

    code_pattern = re.compile(r"(?P<quote>['\"])(?P<code>[A-Z][A-Z0-9_]+)(?P=quote)\s*:\s*\{")
    for match in code_pattern.finditer(raw_html):
        code = match.group("code")
        extracted = extract_balanced_object(raw_html, match.end() - 1)
        if not extracted:
            continue
        fragment, _ = extracted
        parsed_fragment: dict[str, object] = {}
        try:
            candidate_fragment = json.loads(fragment)
            if isinstance(candidate_fragment, dict):
                parsed_fragment = candidate_fragment
        except json.JSONDecodeError:
            pass

        def scalar(*names: str) -> str | None:
            for name in names:
                found = re.search(
                    rf"['\"]{re.escape(name)}['\"]\s*:\s*['\"](?P<value>[^'\"]*)['\"]",
                    fragment,
                    re.I,
                )
                if found:
                    return html.unescape(found.group("value")).strip() or None
            return None

        title = scalar("title", "formLabel", "listLabel", "EDIT_FORM_LABEL")
        field_type = scalar("type", "dataType", "USER_TYPE_ID")
        multiple_match = re.search(
            r"['\"](?:multiple|MULTIPLE)['\"]\s*:\s*(?P<value>true|false|['\"][YN]['\"])",
            fragment,
            re.I,
        )
        record = ensure_record(code, raw_fields.get(code))
        if title:
            record["field_title"] = title
        if field_type:
            record["field_type"] = field_type
        if multiple_match:
            record["multiple"] = multiple_match.group("value").strip("'\"").lower() in {"true", "y"}
        option_source = parsed_fragment.get("items") or parsed_fragment.get("options")
        options: dict[str, str] = {}
        if isinstance(option_source, list):
            for option in option_source:
                if isinstance(option, dict) and (option.get("ID") is not None or option.get("id") is not None):
                    option_id = str(option.get("ID") if option.get("ID") is not None else option.get("id"))
                    option_value = option.get("VALUE") if option.get("VALUE") is not None else option.get("value")
                    options[option_id] = str(option_value or "")
        elif isinstance(option_source, dict):
            options = {str(key): str(value) for key, value in option_source.items()}
        if options:
            record["enumeration_options"] = options
            record["display_options"] = options
            record["option_list_version"] = "sha256:" + hashlib.sha256(
                json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        if title or field_type or multiple_match or options:
            record["metadata_source"] = "BITRIX_EMBEDDED_FIELD_CONFIG"
    return schema


def value_from_signed_field(value: object) -> str:
    if isinstance(value, list):
        values = [value_from_signed_field(item) for item in value]
        return ", ".join(item for item in values if item)
    if isinstance(value, dict):
        raw_value = value.get("VALUE")
        if isinstance(raw_value, list):
            values = [value_from_signed_field(item) for item in raw_value]
            return ", ".join(item for item in values if item)
        if isinstance(raw_value, dict):
            return value_from_signed_field(raw_value)
        if raw_value is not None:
            return str(raw_value)
        return ""
    if value is None:
        return ""
    return str(value)


def display_value_from_field(value: object, schema: dict[str, object]) -> str:
    """Resolve a human display value without replacing the preserved raw value.

    Bitrix may serialize a reference as a signed ``VALUE`` only, as a compound
    object with a label, or with a list embedded in the field definition.  This
    resolution is deliberately generic: it uses only data carried by the card
    and never assumes a particular iblock, element ID, or pilot value.
    """
    if isinstance(value, list):
        values = [display_value_from_field(item, schema) for item in value]
        return ", ".join(item for item in values if item)
    if isinstance(value, dict):
        for key in ("DISPLAY_VALUE", "VALUE_NAME", "TITLE", "NAME", "TEXT", "CAPTION"):
            direct = value.get(key)
            if isinstance(direct, (str, int, float)) and str(direct).strip():
                return str(direct).strip()
        raw_value = value.get("VALUE")
    else:
        raw_value = value
    if isinstance(raw_value, list):
        return display_value_from_field(raw_value, schema)
    raw_text = value_from_signed_field(raw_value)
    options = schema.get("display_options") or schema.get("enumeration_options")
    if isinstance(options, dict) and raw_text in options:
        return str(options[raw_text])
    return raw_text


def normalized_field_value(value: object, schema: dict[str, object]) -> str:
    """Keep enum IDs for enum normalizers, but use labels for iblock references."""
    field_type = str(schema.get("field_type") or "").casefold()
    if field_type in {"iblock_element", "iblock_section"}:
        return display_value_from_field(value, schema)
    return value_from_signed_field(value)


def build_field_records(
    schema: dict[str, dict[str, object]],
    raw_fields: dict[str, object],
    *,
    entity_type: str,
    entity_type_id: str,
    entity_id: str,
    source_url: str,
    read_at: str,
    retrieval_method: str,
) -> list[dict[str, object]]:
    """Emit every registered field, including editor-declared empty fields."""
    records: list[dict[str, object]] = []
    for code in sorted(schema):
        definition = schema[code]
        raw_value = raw_fields.get(code)
        normalized = normalized_field_value(raw_value, definition)
        records.append({
            "field_code": code,
            "field_name": code,
            "field_title": definition.get("field_title"),
            "field_type": definition.get("field_type"),
            "multiple": definition.get("multiple"),
            "settings": definition.get("settings") or {},
            "enumeration_options": definition.get("enumeration_options"),
            "display_options": definition.get("display_options"),
            "option_list_version": definition.get("option_list_version"),
            "raw_value": raw_value,
            "normalized_value": normalized,
            "display_value": display_value_from_field(raw_value, definition),
            "entity_type": entity_type,
            "entity_type_id": entity_type_id,
            "entity_id": entity_id,
            "source_url": source_url,
            "read_at": read_at,
            "retrieval_method": retrieval_method,
            "availability": "PASS" if normalized else "FIELD_EMPTY",
            "schema_metadata_source": definition.get("metadata_source"),
        })
    return records


def enrich_deal_from_detail_data(deal: dict[str, str], data: dict[str, object]) -> None:
    field_map = {
        "title": "TITLE",
        "company": "COMPANY_TITLE",
        "amount": "FORMATTED_OPPORTUNITY_WITH_CURRENCY",
        "date_create": "DATE_CREATE",
        "contact": "CONTACT_FULL_NAME",
    }
    for target, source in field_map.items():
        value = value_from_signed_field(data.get(source))
        if value:
            deal[target] = value
    responsible = value_from_signed_field(data.get("ASSIGNED_BY_FORMATTED_NAME"))
    if responsible:
        deal["responsible"] = responsible
    stage = value_from_signed_field(data.get("STAGE_ID"))
    category = value_from_signed_field(data.get("CATEGORY_NAME"))
    if stage and category:
        deal["stage"] = f"{category}: {stage}"
    elif stage or category:
        deal["stage"] = stage or category
    comments = strip_bbcode(value_from_signed_field(data.get("COMMENTS")))
    if comments:
        deal["comments"] = comments[:800]


def extract_deal_id_from_path(path: str) -> str | None:
    match = re.search(r"/crm/deal/details/(?P<id>\d+)/", path)
    return match.group("id") if match else None


def merge_deal_match(
    deals: list[dict[str, str]],
    deal: dict[str, str],
) -> None:
    deal_id = deal.get("id", "")
    if not deal_id:
        return
    for existing in deals:
        if existing.get("id") != deal_id:
            continue
        for key, value in deal.items():
            if value and not existing.get(key):
                existing[key] = value
        return
    deals.append(deal)


def collect_deal_matches_from_links(
    links: Iterable[str],
    title_by_link: dict[str, str],
    company_name: str,
) -> list[dict[str, str]]:
    deals: list[dict[str, str]] = []
    for link in links:
        deal_id = extract_deal_id_from_path(link)
        if not deal_id:
            continue
        merge_deal_match(
            deals,
            {
                "id": deal_id,
                "title": title_by_link.get(link, ""),
                "company": company_name,
                "stage": "",
                "responsible": "",
                "amount": "",
                "date_create": "",
                "contact": "",
                "url": f"/crm/deal/details/{deal_id}/",
                "source": "timeline",
            },
        )
    return deals


def parse_company_rows(raw_html: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    row_re = re.compile(
        r'<tr class="main-grid-row main-grid-row-body".*?data-id="(?P<id>\d+)".*?</tr>',
        re.S,
    )
    for match in row_re.finditer(raw_html):
        row_html = match.group(0)
        company_id = match.group("id")
        title_match = re.search(r'href="/crm/company/details/\d+/">(?P<title>.*?)</a>', row_html, re.S)
        type_match = re.search(
            r'<td class="main-grid-cell main-grid-cell-left"[^>]*><div class="main-grid-cell-inner"><span class="main-grid-cell-content"[^>]*>(?P<type>.*?)</span>',
            row_html,
            re.S,
        )
        title = strip_tags(title_match.group("title")) if title_match else ""
        company_type = strip_tags(type_match.group("type")) if type_match else ""
        rows.append(
            {
                "id": company_id,
                "title": title,
                "type": company_type,
                "url": f"/crm/company/details/{company_id}/",
            }
        )
    return rows


def extract_file_links(raw_html: str) -> list[str]:
    matches = re.findall(r'href="([^"]+)"', raw_html, re.I)
    file_links: list[str] = []
    for link in matches:
        if any(token in link.lower() for token in ["/upload/", "download", "file="]):
            file_links.append(link)
            continue
        if re.search(r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|txt|rtf|jpg|jpeg|png)$", link, re.I):
            file_links.append(link)
    deduped: list[str] = []
    seen: set[str] = set()
    for link in file_links:
        if link in seen:
            continue
        seen.add(link)
        deduped.append(link)
    return deduped


def js_unescape(value: str) -> str:
    return html.unescape(value).replace("\\/", "/").replace("\\u0026", "&")


def guess_extension(content: bytes, fallback: str = ".bin") -> str:
    if content.startswith(b"%PDF"):
        return ".pdf"
    if content.startswith(b"PK\x03\x04"):
        # OOXML, XLSX, PPTX and ordinary ZIP archives share this signature.
        # Do not mislabel an unknown ZIP as a Word document.
        return ".zip"
    if content.startswith(b"\xd0\xcf\x11\xe0"):
        return ".doc"
    return fallback


def field_name_to_model_key(field_name: str) -> str:
    parts = field_name.lower().split("_")
    if not parts:
        return field_name
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def extract_crm_item_file_refs(raw_html: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    template_re = re.compile(
        r"'URL_TEMPLATE':'(?P<template>[^']*crm\.controller\.item\.getFile[^']*fieldName=(?P<field>[^&#']+)[^']*)'"
    )
    for match in template_re.finditer(raw_html):
        template = js_unescape(match.group("template"))
        field_name = match.group("field")
        model_key = field_name_to_model_key(field_name)
        value_match = re.search(rf"'{re.escape(model_key)}'\s*:\s*(?P<value>\[[^\]]*]|\d+|'[^']*')", raw_html)
        if not value_match:
            continue
        raw_value = value_match.group("value")
        file_ids = re.findall(r"\d+", raw_value)
        for file_id in file_ids:
            if not file_id or file_id == "0":
                continue
            refs.append(
                {
                    "field_name": field_name,
                    "file_id": file_id,
                    "url": template.replace("#file_id#", file_id),
                }
            )
    return refs


def extract_shared_disk_folder_links(raw_html: str) -> list[str]:
    links: list[str] = []
    pattern = re.compile(r"(?:https?://[^'\"\s]+)?/docs/shared/path/[^'\"\s]+")
    for match in pattern.finditer(raw_html):
        link = js_unescape(match.group(0)).rstrip("\\")
        if link not in links:
            links.append(link)
    return links


def extract_disk_downloads(raw_html: str) -> list[dict[str, str]]:
    downloads: list[dict[str, str]] = []
    row_re = re.compile(
        r'"id":"(?P<id>\d+)","name":"(?P<name>[^"]+)","isFolder":false.*?'
        r'"href":"(?P<href>\\/disk\\/downloadFile\\/[^"]+)"',
        re.S,
    )
    for match in row_re.finditer(raw_html):
        href = js_unescape(match.group("href"))
        name = js_unescape(match.group("name"))
        downloads.append({"object_id": match.group("id"), "name": name, "url": href})

    href_re = re.compile(r'href":"(?P<href>\\/disk\\/downloadFile\\/(?P<id>\d+)\\/\\?[^"]*filename=(?P<name>[^"&]+))')
    for match in href_re.finditer(raw_html):
        href = js_unescape(match.group("href"))
        name = urllib.parse.unquote_plus(js_unescape(match.group("name")))
        item = {"object_id": match.group("id"), "name": name, "url": href}
        if item not in downloads:
            downloads.append(item)

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in downloads:
        key = (item["object_id"], item["url"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def is_probable_html(content: bytes) -> bool:
    prefix = content.lstrip()[:512].lower()
    return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html") or b"name=\"form_auth\"" in prefix


def disk_entry_extension(name: str) -> str | None:
    suffix = pathlib.Path(name).suffix.lower()
    return suffix or None


def disk_entry_mime(name: str) -> str | None:
    mime, _ = mimetypes.guess_type(name)
    return mime


def extract_shared_disk_child_folders(raw_html: str, current_url: str) -> list[dict[str, str]]:
    """Find child folder tiles, never shared-disk breadcrumb ancestors."""
    current = urllib.parse.urlsplit(current_url)
    discovered: list[dict[str, str]] = []
    seen: set[str] = set()
    entry_re = re.compile(
        r'"id":"(?P<id>\d+)","name":"(?P<name>[^"]+)","isFolder":true.*?"link":"(?P<link>[^"]+)"',
        re.S,
    )
    for match in entry_re.finditer(raw_html):
        candidate = urllib.parse.urljoin(current_url, js_unescape(match.group("link")))
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.scheme and parsed.netloc and (parsed.scheme, parsed.netloc) != (current.scheme, current.netloc):
            continue
        canonical = urllib.parse.urlunsplit((current.scheme, current.netloc, parsed.path.rstrip("/"), "", ""))
        current_canonical = urllib.parse.urlunsplit((current.scheme, current.netloc, current.path.rstrip("/"), "", ""))
        if canonical == current_canonical or canonical in seen:
            continue
        seen.add(canonical)
        name = js_unescape(match.group("name")) or urllib.parse.unquote(parsed.path.rstrip("/").split("/")[-1]) or "folder"
        discovered.append({"name": name, "url": canonical + "/"})
    return discovered


def extract_disk_pagination_links(raw_html: str, current_url: str) -> list[str]:
    """Return explicitly linked page variants; do not invent component routes."""
    current = urllib.parse.urlsplit(current_url)
    links: list[str] = []
    for raw_link in re.findall(r'href=["\']([^"\']+)["\']', raw_html, re.I):
        candidate = urllib.parse.urljoin(current_url, html.unescape(raw_link))
        parsed = urllib.parse.urlsplit(candidate)
        if (parsed.scheme, parsed.netloc, parsed.path.rstrip("/")) != (current.scheme, current.netloc, current.path.rstrip("/")):
            continue
        query = urllib.parse.parse_qs(parsed.query)
        if not any(key.casefold() in {"page", "pagen_1", "pagen_2"} for key in query):
            continue
        canonical = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if canonical not in links:
            links.append(canonical)
    return links


def extract_tab_loaders(raw_html: str) -> list[dict[str, object]]:
    tabs: list[dict[str, object]] = []
    section_match = re.search(r"tabs:\s*\[(?P<body>.*?)]\s*,\s*containerId:", raw_html, re.S)
    if not section_match:
        return tabs
    section = section_match.group("body")
    pattern = re.compile(
        r"\{'id':'(?P<id>[^']+)','name':'(?P<name>[^']+)'(?P<body>.*?)}(?=,\{'id':'|\Z)",
        re.S,
    )
    for match in pattern.finditer(section):
        body = match.group("body")
        service_match = re.search(r"'serviceUrl':'(?P<url>[^']+)'", body)
        component_data: dict[str, object] = {}
        for key in ("template", "signedParameters", "contextId"):
            key_match = re.search(rf"'{key}':'(?P<value>(?:\\'|[^'])*)'", body)
            if key_match:
                component_data[key] = js_unescape(key_match.group("value"))
        params_match = re.search(r"'params':\{(?P<params>.*?)\}\s*(?:\}\}|,\s*'|\Z)", body, re.S)
        if params_match:
            params: dict[str, str] = {}
            for key, value in re.findall(r"'([^']+)':'([^']*)'", params_match.group("params")):
                params[key] = js_unescape(value)
            if params:
                component_data["params"] = params
        query = urllib.parse.parse_qs(urllib.parse.urlparse(service_match.group("url") if service_match else "").query)
        for key in ("entityTypeId", "parentEntityTypeId", "parentEntityId", "site", "sessid"):
            if query.get(key):
                component_data.setdefault(key, query[key][0])
        tabs.append(
            {
                "id": match.group("id"),
                "name": match.group("name"),
                "service_url": service_match.group("url") if service_match else "",
                "component_data": component_data,
            }
        )
    return tabs


def flatten_form_fields(prefix: str, value: object) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        fields: list[tuple[str, str]] = []
        for key, child in value.items():
            fields.extend(flatten_form_fields(f"{prefix}[{key}]", child))
        return fields
    if isinstance(value, list):
        fields = []
        for child in value:
            fields.extend(flatten_form_fields(f"{prefix}[]", child))
        return fields
    return [(prefix, "" if value is None else str(value))]


def extract_redirect_links(raw_html: str) -> list[str]:
    links = re.findall(r'"action":\{"type":"redirect","value":"([^"]+)"\}', raw_html)
    cleaned = [html.unescape(link).replace("\\/", "/") for link in links]
    deduped: list[str] = []
    seen: set[str] = set()
    for link in cleaned:
        if link.endswith("/0/"):
            continue
        if link in seen:
            continue
        seen.add(link)
        deduped.append(link)
    return deduped


def extract_timeline_highlights(raw_html: str) -> list[dict[str, str]]:
    highlights: list[dict[str, str]] = []
    pattern = re.compile(
        r'"header":\{"title":"(?P<header>[^"]+)","date":(?P<date>\d+).*?'
        r'"body":\{"blocks":\{"content":.*?'
        r'"value":"(?P<section>[^"]+)".*?'
        r'"text":"(?P<link_text>[^"]+)".*?'
        r'"action":\{"type":"redirect","value":"(?P<link>[^"]+)"\}',
        re.S,
    )
    for match in pattern.finditer(raw_html):
        timestamp = int(match.group("date"))
        date_text = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        highlights.append(
            {
                "header": match.group("header"),
                "date": date_text,
                "section": match.group("section"),
                "link_text": match.group("link_text"),
                "link": html.unescape(match.group("link")).replace("\\/", "/"),
            }
        )
    return highlights


def extract_contact_communications(raw_html: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    pattern = re.compile(
        r'"addressSource":\{"entityTypeId":3,"entityId":(?P<contact_id>\d+),"categoryId":null},'
        r'"address":\{"id":(?P<address_id>\d+),"typeId":"(?P<type_id>[^"]+)",'
        r'"valueType":"(?P<value_type>[^"]+)","value":"(?P<value>[^"]*)"',
        re.S,
    )
    for match in pattern.finditer(raw_html):
        entries.append(
            {
                "contact_id": match.group("contact_id"),
                "address_id": match.group("address_id"),
                "type": match.group("type_id"),
                "value_type": match.group("value_type"),
                "value": html.unescape(match.group("value")).replace("\\/", "/"),
            }
        )
    return entries


def extract_document_generator_urls(raw_html: str) -> dict[str, str]:
    urls: dict[str, str] = {}
    template_match = re.search(r"'templateListUrl':'([^']+)'", raw_html)
    view_match = re.search(r"'documentUrl':'([^']+)'", raw_html)
    if template_match:
        urls["template_list_url"] = html.unescape(template_match.group(1)).replace("\\/", "/")
    if view_match:
        urls["document_slider_url"] = html.unescape(view_match.group(1)).replace("\\/", "/")
    return urls


def extract_title(raw_html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", raw_html, re.I | re.S)
    return strip_tags(match.group(1)) if match else ""


def make_iframe_path(path: str) -> str:
    parsed = urllib.parse.urlparse(path)
    if not parsed.path:
        return path
    if "/details/" not in parsed.path:
        return path
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query.setdefault("IFRAME", ["Y"])
    query.setdefault("IFRAME_TYPE", ["SIDE_SLIDER"])
    encoded = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse(("", "", parsed.path, "", encoded, ""))


def save_tsv(path: pathlib.Path, rows: list[dict[str, str]], headers: list[str]) -> None:
    lines = ["\t".join(headers)]
    for row in rows:
        lines.append("\t".join(row.get(header, "") for header in headers))
    write_text_file(path, "\n".join(lines) + "\n")


def describe_source(source: str) -> str:
    if "/docs/shared/path/" in source:
        parsed = urllib.parse.urlparse(source)
        folder = urllib.parse.unquote(parsed.path.rstrip("/").split("/")[-1])
        return f"Bitrix Disk, папка `{folder}`"
    if "/page/dogovory/" in source:
        return source
    return source


def ensure_dir(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class BitrixSessionClient:
    def __init__(self, base_url: str, login: str, password: str) -> None:
        self.base_url = normalize_base_url(base_url)
        self.login = login
        self.password = password
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cookies)
        )

    def prepare_request(self, request: str | urllib.request.Request) -> urllib.request.Request:
        default_headers = {
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "Mozilla/5.0 Bitrix24SessionBridge/1.0",
        }
        if isinstance(request, str):
            return urllib.request.Request(request, headers=default_headers)
        for key, value in default_headers.items():
            if not request.has_header(key):
                request.add_header(key, value)
        return request

    def open_with_retry(self, request: str | urllib.request.Request, timeout: int = 30, attempts: int = 5):
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self.opener.open(self.prepare_request(request), timeout=timeout)
            except (
                TimeoutError,
                urllib.error.URLError,
                ssl.SSLError,
                socket.timeout,
                http.client.RemoteDisconnected,
                http.client.IncompleteRead,
            ) as error:
                last_error = error
                if attempt == attempts:
                    break
                time.sleep(min(12, 1.5 * attempt))
        if last_error:
            raise last_error
        raise RuntimeError("Не удалось выполнить запрос")

    def login_portal(self) -> str:
        self.open_with_retry(self.base_url + "/", timeout=30).read()
        payload = urllib.parse.urlencode(
            {
                "AUTH_FORM": "Y",
                "TYPE": "AUTH",
                "backurl": "/",
                "USER_LOGIN": self.login,
                "USER_PASSWORD": self.password,
                "USER_REMEMBER": "N",
            }
        ).encode()
        request = urllib.request.Request(
            self.base_url + "/?login=yes",
            data=payload,
            method="POST",
        )
        with self.open_with_retry(request, timeout=30) as response:
            body = response.read().decode("utf-8", "ignore")
            final_url = response.geturl()
        if 'name="form_auth"' in body:
            raise RuntimeError("Не удалось выполнить вход в Bitrix24: форма авторизации вернулась повторно")
        return final_url

    def fetch(self, url_or_path: str) -> tuple[str, str]:
        url = to_absolute(self.base_url, url_or_path)
        with self.open_with_retry(url, timeout=30) as response:
            body = response.read().decode("utf-8", "ignore")
            final_url = response.geturl()
        return final_url, body

    def post_form(
        self,
        url_or_path: str,
        fields: list[tuple[str, str]],
        timeout: int = 30,
        attempts: int = 2,
    ) -> tuple[str, str]:
        url = to_absolute(self.base_url, url_or_path)
        payload = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        with self.open_with_retry(request, timeout=timeout, attempts=attempts) as response:
            body = response.read().decode("utf-8", "ignore")
            final_url = response.geturl()
        return final_url, body

    def fetch_binary(self, url_or_path: str, max_bytes: int | None = None) -> tuple[str, bytes]:
        url = to_absolute(self.base_url, url_or_path)
        limit = max_bytes if max_bytes is not None else env_int("B24_MAX_DOWNLOAD_BYTES", DEFAULT_MAX_DOWNLOAD_BYTES)
        with self.open_with_retry(url, timeout=30, attempts=2) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > limit:
                raise RuntimeError(f"Файл больше лимита скачивания: {int(length)} байт > {limit} байт")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise RuntimeError(f"Файл больше лимита скачивания: {total} байт > {limit} байт")
            body = b"".join(chunks)
            final_url = response.geturl()
        return final_url, body

    def fetch_deal_grid_page(self, page: int, bxajaxid: str) -> tuple[str, str]:
        path = f"/crm/deal/list/?by=&order=&page={page}&bxajaxid={bxajaxid}"
        return self.fetch(path)


def render_links(raw_html: str) -> Iterable[str]:
    parser = LinkParser()
    parser.feed(raw_html)
    seen: set[str] = set()
    for link in parser.links:
        if link in seen:
            continue
        seen.add(link)
        yield link


def command_probe(client: BitrixSessionClient) -> int:
    final_url = client.login_portal()
    print(f"login_ok final_url={final_url}")
    probe_paths = [
        "/market/hooks/",
        "/market/category/local/",
        "/market/",
    ]
    for path in probe_paths:
        try:
            url, body = client.fetch(path)
            auth_form = 'name="form_auth"' in body
            print(f"probe path={path} final_url={url} auth_form={auth_form} size={len(body)}")
        except Exception as exc:
            print(f"probe path={path} error={type(exc).__name__}:{exc}")
    return 0


def command_fetch(client: BitrixSessionClient, target: str, fmt: str) -> int:
    client.login_portal()
    final_url, body = client.fetch(target)
    print(f"final_url={final_url}", file=sys.stderr)
    if fmt == "html":
        sys.stdout.write(body)
        return 0
    if fmt == "text":
        sys.stdout.write(clean_text(body) + "\n")
        return 0
    if fmt == "links":
        for link in render_links(body):
            print(link)
        return 0
    raise SystemExit(f"Неподдерживаемый формат: {fmt}")


def command_contract(output: str | None) -> int:
    payload = {
        "schema_version": "1.0",
        "bridge_name": "bitrix24-session-bridge",
        "bridge_version": BRIDGE_VERSION,
        "contract_version": BRIDGE_CONTRACT_VERSION,
        "capabilities": list(BRIDGE_CAPABILITIES),
        "commands": [
            "collect-deal-context",
            "collect-entity-context",
            "collect-project-folder",
            "collect-company-context",
            "list-income-contracts",
        ],
        "read_only": True,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output:
        write_text_file(pathlib.Path(output).expanduser().resolve(), rendered)
    else:
        sys.stdout.write(rendered)
    return 0


def command_collect_entity_context(
    client: BitrixSessionClient,
    output_dir: str,
    entity_url: str,
    expected_kind: str | None,
) -> int:
    """Collect one explicitly selected CRM entity; never search for a substitute."""
    root = pathlib.Path(output_dir).expanduser().resolve()
    raw_dir = ensure_dir(root / "raw")
    meta_dir = ensure_dir(root / "metadata")
    started_at = now_iso()
    parsed = urllib.parse.urlparse(entity_url)
    ref = classify_entity_path(parsed.path)
    errors: list[str] = []
    if ref is None:
        errors.append("ENTITY_URL_INVALID")
    elif expected_kind and ref.get("kind") != expected_kind:
        errors.append("ENTITY_KIND_MISMATCH")
    if errors:
        report = {
            "schema_version": "1.0",
            "operation": "COLLECT_ENTITY_CONTEXT",
            "status": "blocked",
            "bridge_version": BRIDGE_VERSION,
            "bridge_contract_version": BRIDGE_CONTRACT_VERSION,
            "errors": errors,
            "started_at": started_at,
            "finished_at": now_iso(),
        }
        write_text_file(meta_dir / "run_report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return 2

    assert ref is not None
    expected_type_id = str(ref["entity_type_id"])
    expected_id = str(ref["id"])
    attempts: list[dict[str, object]] = []

    # Unlike the deal collector, this command used to fetch a card before a
    # session existed.  That returned an auth shell for contacts/companies and
    # made an exact entity look absent.  Login is a read-only prerequisite.
    client.login_portal()

    def exact(page_html: str) -> list[dict[str, object]]:
        return [
            item for item in extract_entity_data_objects(page_html)
            if str(item.get("entity_type_id")) == expected_type_id
            and isinstance(item.get("data"), dict)
            and value_from_signed_field(item["data"].get("ID")) == expected_id
        ]

    outer_url, outer_html = client.fetch(entity_url)
    write_text_file(raw_dir / "entity-outer.html", outer_html)
    matches = exact(outer_html)
    attempts.append({"kind": "OUTER", "url": outer_url, "exact_model_count": len(matches)})
    final_url, raw_html = outer_url, outer_html
    if len(matches) != 1:
        iframe_url, iframe_html = client.fetch(make_iframe_path(entity_url))
        write_text_file(raw_dir / "entity-iframe.html", iframe_html)
        iframe_matches = exact(iframe_html)
        attempts.append({"kind": "SIDE_SLIDER", "url": iframe_url, "exact_model_count": len(iframe_matches)})
        if len(iframe_matches) == 1:
            final_url, raw_html, matches = iframe_url, iframe_html, iframe_matches

    if len(matches) != 1:
        errors.append("ENTITY_EXACT_MACHINE_MODEL_NOT_UNIQUE")
        raw_fields: dict[str, object] = {}
    else:
        raw_fields = dict(matches[0]["data"])
    schema = extract_field_schema(raw_html, raw_fields)
    read_at = now_iso()
    fields = build_field_records(
        schema, raw_fields,
        entity_type=str(ref["kind"]), entity_type_id=expected_type_id,
        entity_id=expected_id, source_url=final_url, read_at=read_at,
        retrieval_method="AUTHENTICATED_ENTITY_CARD_EMBEDDED_MODEL",
    )
    entity = {
        "schema_version": "1.0",
        "entity_type": ref["kind"],
        "entity_type_id": expected_type_id,
        "entity_id": expected_id,
        "source_url": final_url,
        "read_at": read_at,
        "raw_html_sha256": hashlib.sha256(raw_html.encode("utf-8")).hexdigest(),
        "fetch_attempts": attempts,
    }
    related_entities = extract_entity_refs(raw_html, source="ENTITY_CARD_EMBEDDED_RELATION")
    for related in related_entities:
        related["source_entity_type"] = ref["kind"]
        related["source_entity_type_id"] = expected_type_id
        related["source_entity_id"] = expected_id
    write_text_file(meta_dir / "entity.json", json.dumps(entity, ensure_ascii=False, indent=2) + "\n")
    write_text_file(meta_dir / "fields.json", json.dumps(fields, ensure_ascii=False, indent=2) + "\n")
    write_text_file(meta_dir / "field_schema.json", json.dumps(list(schema.values()), ensure_ascii=False, indent=2) + "\n")
    write_text_file(meta_dir / "related_entities.json", json.dumps(related_entities, ensure_ascii=False, indent=2) + "\n")
    status = "ok" if fields and not errors else "blocked"
    report = {
        "schema_version": "1.0",
        "operation": "COLLECT_ENTITY_CONTEXT",
        "status": status,
        "bridge_version": BRIDGE_VERSION,
        "bridge_contract_version": BRIDGE_CONTRACT_VERSION,
        "selected_entity": entity,
        "counts": {"fields": len(fields), "field_schema": len(schema), "related_entities": len(related_entities)},
        "fetch_attempts": attempts,
        "errors": errors,
        "started_at": started_at,
        "finished_at": now_iso(),
    }
    write_text_file(meta_dir / "run_report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(str(meta_dir / "run_report.json"))
    return 0 if status == "ok" else 2


def command_collect_project_folder(
    client: BitrixSessionClient,
    output_dir: str,
    folder_url: str,
    download_file_urls: list[str],
    max_pages: int,
) -> int:
    """Inventory one exact Bitrix Disk project folder through the logged-in session.

    The command intentionally has no business rule for selecting a document.
    It inventories all navigable pages and nested shared folders, then downloads
    only file URLs explicitly requested by the caller and found in that exact
    inventory.  Every network operation remains GET/read-only.
    """
    root = pathlib.Path(output_dir).expanduser().resolve()
    raw_dir = ensure_dir(root / "raw")
    downloads_dir = ensure_dir(root / "downloads")
    started_at = now_iso()
    errors: list[str] = []
    warnings: list[str] = []
    parsed_root = urllib.parse.urlsplit(folder_url)
    if not parsed_root.scheme or not parsed_root.netloc or "/docs/shared/path/" not in parsed_root.path:
        errors.append("PROJECT_FOLDER_URL_INVALID")
    if errors:
        report = {
            "schema_version": "1.0", "operation": "COLLECT_PROJECT_FOLDER", "status": "blocked",
            "started_at": started_at, "finished_at": now_iso(), "errors": errors, "warnings": warnings,
        }
        write_text_file(root / "run_report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return 2

    client.login_portal()
    queue: list[tuple[str, str, str | None]] = [(folder_url, "", None)]
    visited: set[str] = set()
    page_count = 0
    folders: list[dict[str, object]] = []
    files: list[dict[str, object]] = []
    raw_pages: list[dict[str, object]] = []
    known_downloads: dict[str, dict[str, object]] = {}

    while queue and page_count < max_pages:
        target, parent_path, parent_id = queue.pop(0)
        parsed = urllib.parse.urlsplit(target)
        canonical = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/", parsed.query, ""))
        if canonical in visited:
            continue
        visited.add(canonical)
        try:
            final_url, page_html = client.fetch(target)
        except Exception as exc:
            errors.append(f"PROJECT_FOLDER_FETCH_FAILED:{type(exc).__name__}")
            continue
        page_count += 1
        raw_name = f"folder-page-{page_count:03d}.html"
        write_text_file(raw_dir / raw_name, page_html)
        raw_pages.append({"url": final_url, "raw_file": str(pathlib.Path("raw") / raw_name), "read_at": now_iso()})
        if 'name="form_auth"' in page_html:
            errors.append("PROJECT_FOLDER_AUTH_HTML")
            continue
        if re.search(r"(?:access denied|доступ запрещен|нет прав)", clean_text(page_html), re.I):
            errors.append("PROJECT_FOLDER_ACCESS_DENIED")
            continue

        folder_name = urllib.parse.unquote(urllib.parse.urlsplit(final_url).path.rstrip("/").split("/")[-1]) or "project-folder"
        folders.append({
            "object_id": parent_id,
            "folder_id": parent_id,
            "path": parent_path or folder_name,
            "name": folder_name,
            "kind": "folder",
            "source_url": final_url,
            "read_at": raw_pages[-1]["read_at"],
            "retrieval_method": "AUTHENTICATED_BITRIX_DISK_FOLDER_PAGE",
        })
        for item in extract_disk_downloads(page_html):
            name = item["name"]
            download_url = urllib.parse.urljoin(final_url, item["url"])
            record: dict[str, object] = {
                "object_id": item.get("object_id"),
                "folder_id": parent_id,
                "path": parent_path or folder_name,
                "name": name,
                "kind": "file",
                "mime_type": disk_entry_mime(name),
                "extension": disk_entry_extension(name),
                "size": None,
                "modified_at": None,
                "version": None,
                "source_url": final_url,
                "download_url": download_url,
                "read_at": raw_pages[-1]["read_at"],
                "retrieval_method": "AUTHENTICATED_BITRIX_DISK_FOLDER_PAGE",
                "download_status": "NOT_REQUESTED",
            }
            key = str(item.get("object_id") or download_url)
            if key not in known_downloads:
                known_downloads[key] = record
                files.append(record)
        for child in extract_shared_disk_child_folders(page_html, final_url):
            child_path = "/".join(part for part in (parent_path, child["name"]) if part)
            queue.append((child["url"], child_path, None))
        for page_url in extract_disk_pagination_links(page_html, final_url):
            queue.append((page_url, parent_path, parent_id))

    if queue:
        warnings.append("PROJECT_FOLDER_INVENTORY_PAGE_LIMIT_REACHED")

    requested = {urllib.parse.urlsplit(url).path.rstrip("/") for url in download_file_urls}
    for record in files:
        download_url = str(record.get("download_url") or "")
        parsed_download = urllib.parse.urlsplit(download_url)
        if parsed_download.path.rstrip("/") not in requested and download_url not in download_file_urls:
            continue
        try:
            final_url, content = client.fetch_binary(download_url)
        except Exception as exc:
            record["download_status"] = "DOWNLOAD_FAILED"
            record["download_error"] = f"{type(exc).__name__}:{exc}"
            continue
        if is_probable_html(content):
            record["download_status"] = "HTML_INSTEAD_OF_FILE"
            record["download_error"] = "AUTH_OR_ERROR_HTML"
            errors.append("PROJECT_FOLDER_FILE_HTML_INSTEAD_OF_BINARY")
            continue
        safe_name = safe_filename_from_url(str(record.get("name") or "file"), "file")
        target = downloads_dir / safe_name
        suffix = 2
        while target.exists():
            target = downloads_dir / f"{target.stem}-{suffix}{target.suffix}"
            suffix += 1
        save_binary_file(target, content)
        record.update({
            "download_status": "DOWNLOADED",
            "download_url": final_url,
            "local_file": str(target.relative_to(root)),
            "size": len(content),
            "mime_type": disk_entry_mime(target.name) or record.get("mime_type"),
            "sha256": hashlib.sha256(content).hexdigest(),
        })

    inventory = {
        "schema_version": "1.0",
        "operation": "COLLECT_PROJECT_FOLDER",
        "root_folder_url": folder_url,
        "read_only": True,
        "collected_at": now_iso(),
        "coverage": {
            "pages_collected": page_count,
            "folders_collected": len(folders),
            "files_collected": len(files),
            "page_limit": max_pages,
            "complete": not bool(queue),
        },
        "folders": folders,
        "files": files,
        "raw_pages": raw_pages,
        "errors": errors,
        "warnings": warnings,
    }
    write_text_file(root / "project-folder-files.json", json.dumps(inventory, ensure_ascii=False, indent=2) + "\n")
    report = {
        "schema_version": "1.0", "operation": "COLLECT_PROJECT_FOLDER",
        "status": "ok" if not errors and not queue else "partial" if not errors else "blocked",
        "bridge_version": BRIDGE_VERSION, "bridge_contract_version": BRIDGE_CONTRACT_VERSION,
        "inventory": str(root / "project-folder-files.json"), "started_at": started_at,
        "finished_at": now_iso(), "errors": errors, "warnings": warnings,
    }
    write_text_file(root / "run_report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(str(root / "project-folder-files.json"))
    return 0 if report["status"] == "ok" else 2


def command_collect_deal_context(
    client: BitrixSessionClient,
    output_dir: str,
    project_number: str | None,
    deal_id: str | None,
    deal_url: str | None,
    skip_document_downloads: bool,
) -> int:
    """Collect one exact deal as reusable read-only machine data.

    The command exposes only portal mechanics: raw deal fields, linked
    entities, file references, lazy-tab descriptors, provenance, and a
    completeness report. It does not calculate downstream business readiness.
    """
    root = pathlib.Path(output_dir).expanduser().resolve()
    raw_dir = ensure_dir(root / "raw")
    meta_dir = ensure_dir(root / "metadata")
    documents_dir = ensure_dir(root / "documents")
    started_at = now_iso()
    errors: list[str] = []
    warnings: list[str] = []
    candidates: list[dict[str, str]] = []
    selected: dict[str, str] | None = None

    client.login_portal()
    if project_number:
        number = normalize_four_digit_project_number(project_number)
        search_path = "/crm/deal/list/?" + urllib.parse.urlencode({"FIND": number})
        search_url, search_html = client.fetch(search_path)
        pattern = re.compile(rf"^\s*{re.escape(number)}\s*:")
        by_id = {
            row["id"]: row
            for row in parse_deal_rows(search_html)
            if pattern.match(row.get("title", ""))
        }
        candidates = [
            {
                "deal_id": row["id"],
                "title": row.get("title", ""),
                "deal_url": to_absolute(client.base_url, row["url"]),
            }
            for row in by_id.values()
        ]
        write_text_file(raw_dir / "deal-search.html", search_html)
        if len(candidates) == 1:
            selected = candidates[0]
        elif not candidates:
            errors.append("ENTITY_NOT_FOUND")
        else:
            errors.append("ENTITY_NOT_UNIQUE")
        deal_search_source = search_url
    elif deal_id:
        normalized_id = str(deal_id).strip()
        if not normalized_id.isdigit():
            errors.append("DEAL_ID_INVALID")
        else:
            selected = {
                "deal_id": normalized_id,
                "title": "",
                "deal_url": to_absolute(client.base_url, f"/crm/deal/details/{normalized_id}/"),
            }
        deal_search_source = "DIRECT_DEAL_ID"
    else:
        canonical = canonical_deal_url(deal_url)
        if not canonical:
            errors.append("DEAL_URL_INVALID")
        else:
            normalized_id = extract_deal_id_from_path(urllib.parse.urlparse(canonical).path) or ""
            selected = {"deal_id": normalized_id, "title": "", "deal_url": canonical}
        deal_search_source = "DIRECT_DEAL_URL"

    if selected is None:
        report = {
            "schema_version": "1.0",
            "operation": "COLLECT_DEAL_CONTEXT",
            "status": "blocked",
            "started_at": started_at,
            "finished_at": now_iso(),
            "search_source": deal_search_source,
            "candidates": candidates,
            "errors": errors,
            "warnings": warnings,
        }
        write_text_file(meta_dir / "run_report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(str(meta_dir / "run_report.json"))
        return 2

    deal_id_value = selected["deal_id"]
    outer_url, outer_html = client.fetch(selected["deal_url"])
    if 'name="form_auth"' in outer_html:
        errors.append("SOURCE_UNAVAILABLE")
    write_text_file(raw_dir / "deal-outer.html", outer_html)
    write_text_file(raw_dir / "deal-outer.txt", clean_text(outer_html) + "\n")

    attempts: list[dict[str, object]] = []

    def exact_deal_objects(page_html: str) -> list[dict[str, object]]:
        objects = extract_entity_data_objects(page_html)
        return [
            item for item in objects
            if str(item.get("entity_type_id")) == "2"
            and isinstance(item.get("data"), dict)
            and value_from_signed_field(item["data"].get("ID")) == deal_id_value
        ]

    final_url, raw_html = outer_url, outer_html
    exact_objects = exact_deal_objects(outer_html)
    attempts.append({"kind": "OUTER", "url": outer_url, "exact_model_count": len(exact_objects)})
    if len(exact_objects) != 1:
        iframe_target = make_iframe_path(selected["deal_url"])
        iframe_url, iframe_html = client.fetch(iframe_target)
        write_text_file(raw_dir / "deal-iframe.html", iframe_html)
        write_text_file(raw_dir / "deal-iframe.txt", clean_text(iframe_html) + "\n")
        iframe_exact = exact_deal_objects(iframe_html)
        attempts.append({"kind": "SIDE_SLIDER", "url": iframe_url, "exact_model_count": len(iframe_exact)})
        if len(iframe_exact) == 1:
            final_url, raw_html, exact_objects = iframe_url, iframe_html, iframe_exact

    # Compatibility aliases retained for existing consumers.
    write_text_file(raw_dir / "deal.html", raw_html)
    write_text_file(raw_dir / "deal.txt", clean_text(raw_html) + "\n")

    selected_object = exact_objects[0] if len(exact_objects) == 1 else None
    if selected_object is None:
        errors.append("DEAL_EXACT_MACHINE_MODEL_NOT_UNIQUE")
        raw_fields: dict[str, object] = {}
    else:
        raw_fields = dict(selected_object.get("data", {}))
        selected["title"] = value_from_signed_field(raw_fields.get("TITLE")) or selected.get("title", "")

    read_at = now_iso()
    field_schema = extract_field_schema(raw_html, raw_fields)
    fields = build_field_records(
        field_schema, raw_fields,
        entity_type="deal", entity_type_id="2", entity_id=deal_id_value,
        source_url=final_url, read_at=read_at,
        retrieval_method="AUTHENTICATED_DEAL_CARD_EMBEDDED_MODEL",
    )
    file_refs = extract_crm_item_file_refs(raw_html)
    direct_file_links = extract_file_links(raw_html)
    documents: list[dict[str, object]] = []
    for ref in file_refs:
        record: dict[str, object] = {
            "field_name": ref["field_name"],
            "file_id": ref["file_id"],
            "source_url": to_absolute(client.base_url, ref["url"]),
            "retrieval_method": "CRM_CONTROLLER_ITEM_GET_FILE",
            "availability": "REFERENCE_ONLY" if skip_document_downloads else "PENDING",
        }
        if not skip_document_downloads:
            try:
                resolved_url, content = client.fetch_binary(ref["url"])
                extension = guess_extension(content)
                file_name = f"{slugify(ref['field_name'])}_{ref['file_id']}{extension}"
                destination = documents_dir / file_name
                save_binary_file(destination, content)
                record.update({
                    "resolved_url": resolved_url,
                    "local_path": str(pathlib.Path("documents") / file_name),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "availability": "PASS",
                })
            except Exception as exc:
                record["availability"] = "SOURCE_UNAVAILABLE"
                record["error"] = error_text(exc)
                warnings.append(f"FILE_DOWNLOAD_FAILED:{ref['field_name']}:{ref['file_id']}")
        documents.append(record)
    for url in direct_file_links:
        absolute = to_absolute(client.base_url, url)
        if any(item.get("source_url") == absolute for item in documents):
            continue
        documents.append({
            "source_url": absolute,
            "retrieval_method": "DEAL_CARD_DIRECT_LINK",
            "availability": "REFERENCE_ONLY",
        })

    related = extract_entity_refs(raw_html, final_url)
    for related_ref in related:
        related_ref["source_entity_type"] = "deal"
        related_ref["source_entity_type_id"] = "2"
        related_ref["source_entity_id"] = deal_id_value
    tabs = extract_tab_loaders(raw_html)
    snapshot = {
        "schema_version": "1.0",
        "entity_type": "deal",
        "entity_id": deal_id_value,
        "title": selected.get("title", ""),
        "source_url": final_url,
        "read_at": read_at,
        "retrieval_method": "BITRIX24_SESSION_BRIDGE",
        "raw_html_sha256": hashlib.sha256(raw_html.encode("utf-8")).hexdigest(),
        "outer_url": outer_url,
        "fetch_attempts": attempts,
    }
    write_text_file(meta_dir / "deal.json", json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    write_text_file(meta_dir / "fields.json", json.dumps(fields, ensure_ascii=False, indent=2) + "\n")
    write_text_file(meta_dir / "field_schema.json", json.dumps(list(field_schema.values()), ensure_ascii=False, indent=2) + "\n")
    write_text_file(meta_dir / "files.json", json.dumps(documents, ensure_ascii=False, indent=2) + "\n")
    write_text_file(meta_dir / "related_entities.json", json.dumps(related, ensure_ascii=False, indent=2) + "\n")
    write_text_file(meta_dir / "tabs.json", json.dumps(tabs, ensure_ascii=False, indent=2) + "\n")

    status = "ok" if fields and not errors else ("partial" if fields else "blocked")
    report = {
        "schema_version": "1.0",
        "operation": "COLLECT_DEAL_CONTEXT",
        "bridge_version": BRIDGE_VERSION,
        "bridge_contract_version": BRIDGE_CONTRACT_VERSION,
        "status": status,
        "started_at": started_at,
        "finished_at": now_iso(),
        "search_source": deal_search_source,
        "selected_deal": selected,
        "candidates": candidates,
        "counts": {
            "fields": len(fields),
            "field_schema": len(field_schema),
            "files": len(documents),
            "related_entities": len(related),
            "lazy_tabs_described": len(tabs),
        },
        "errors": errors,
        "warnings": warnings,
        "fetch_attempts": attempts,
    }
    write_text_file(meta_dir / "run_report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(str(meta_dir / "run_report.json"))
    return 0 if status == "ok" else 2


def command_list_deals(client: BitrixSessionClient, client_contains: str | None, max_pages: int) -> int:
    client.login_portal()
    _, first_page_html = client.fetch("/crm/deal/list/")
    bxajaxid = extract_bxajaxid(first_page_html)
    sessid = extract_sessid(first_page_html)
    print(f"grid=CRM_DEAL_LIST_V12 bxajaxid={bxajaxid}" + (f" sessid={sessid}" if sessid else ""), file=sys.stderr)

    needle = client_contains.lower() if client_contains else None
    seen_ids: set[str] = set()
    for page in range(1, max_pages + 1):
        _, page_html = client.fetch_deal_grid_page(page, bxajaxid)
        rows = parse_deal_rows(page_html)
        if not rows:
            break
        new_rows = [row for row in rows if row["id"] not in seen_ids]
        if not new_rows:
            break
        for row in new_rows:
            seen_ids.add(row["id"])
            if needle and needle not in row["company"].lower():
                continue
            print(
                f'{row["id"]}\t{row["company"]}\t{row["title"]}\t{row["stage"]}\t{row["responsible"]}\t{row["url"]}'
            )
    return 0


def command_list_companies(client: BitrixSessionClient, name_contains: str | None, max_pages: int) -> int:
    client.login_portal()
    needle = name_contains.lower() if name_contains else None
    seen_ids: set[str] = set()
    for page in range(1, max_pages + 1):
        _, page_html = client.fetch(f"/crm/company/list/?page={page}")
        rows = parse_company_rows(page_html)
        if not rows:
            break
        new_rows = [row for row in rows if row["id"] not in seen_ids]
        if not new_rows:
            break
        for row in new_rows:
            seen_ids.add(row["id"])
            if needle and needle not in row["title"].lower():
                continue
            print(f'{row["id"]}\t{row["title"]}\t{row["type"]}\t{row["url"]}')
    return 0


def normalized_company_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def is_company_card_url_for_id(
    value: object, company_id: object, base_url: object = ""
) -> bool:
    parsed = urllib.parse.urlparse(str(value or "").strip())
    base = urllib.parse.urlparse(str(base_url or "").strip())
    expected_path = f"/crm/company/details/{str(company_id or '').strip()}/"
    host_matches = not parsed.netloc or not base.netloc or parsed.netloc == base.netloc
    return (
        bool(str(company_id or "").strip())
        and host_matches
        and parsed.path == expected_path
    )


def company_card_fetch_candidates(company: dict[str, str]) -> list[str]:
    """Prefer a saved card URL and retry every plain details URL as a side slider."""
    company_id = str(company.get("id") or "").strip()
    saved_url = str(company.get("url") or "").strip()
    canonical_path = f"/crm/company/details/{company_id}/"
    candidates: list[str] = []
    for value in (
        saved_url,
        make_iframe_path(saved_url),
        canonical_path,
        make_iframe_path(canonical_path),
    ):
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def collect_company_card_income_contracts(
    client: BitrixSessionClient,
    company: dict[str, str],
    max_pages: int,
) -> dict[str, object]:
    """Read the income-contract grid from one exact company card without files."""
    company_id = str(company.get("id") or "").strip()
    company_name = re.sub(r"\s+", " ", str(company.get("title") or "")).strip()
    company_url = ""
    company_html = ""
    tab_candidates: list[dict[str, object]] = []
    attempted_urls: list[str] = []
    company_name_confirmed = False
    ambiguous_tab_seen = False
    for company_path in company_card_fetch_candidates(company):
        candidate_url, candidate_html = client.fetch(company_path)
        attempted_urls.append(candidate_url)
        if normalized_company_name(company_name) not in normalized_company_name(
            clean_text(candidate_html)
        ):
            continue
        company_name_confirmed = True
        candidates = [
            tab
            for tab in extract_tab_loaders(candidate_html)
            if str(tab.get("id") or "") == "tab_relation_dynamic_142"
            or "entityTypeId=142" in str(tab.get("service_url") or "")
            or normalized_company_name(tab.get("name")) == "доходные договоры"
        ]
        if len(candidates) == 1:
            company_url = candidate_url
            company_html = candidate_html
            tab_candidates = candidates
            break
        if len(candidates) > 1:
            ambiguous_tab_seen = True

    if not tab_candidates:
        return {
            "status": "BLOCKED",
            "blockers": [
                "CRM_EXACT_COMPANY_CARD_NAME_NOT_CONFIRMED"
                if not company_name_confirmed
                else (
                    "CRM_COMPANY_INCOME_CONTRACT_TAB_AMBIGUOUS"
                    if ambiguous_tab_seen
                    else "CRM_COMPANY_INCOME_CONTRACT_TAB_NOT_FOUND"
                )
            ],
            "company_card_url": attempted_urls[-1] if attempted_urls else "",
            "company_card_attempted_urls": attempted_urls,
            "rows": [],
        }

    tab = tab_candidates[0]
    tab_id = str(tab.get("id") or "")
    tab_name = re.sub(r"\s+", " ", str(tab.get("name") or "")).strip()
    if normalized_company_name(tab_name) != "доходные договоры":
        return {
            "status": "BLOCKED",
            "blockers": ["CRM_COMPANY_INCOME_CONTRACT_TAB_NAME_NOT_CONFIRMED"],
            "company_card_url": company_url,
            "tab_id": tab_id,
            "rows": [],
        }
    service_url = str(tab.get("service_url") or "")
    component_data = tab.get("component_data")
    if isinstance(component_data, dict) and component_data:
        params = dict(component_data)
        params["TAB_ID"] = tab_id
        fields = [("LOADER_ID", slugify(f"company-{company_id}-{tab_id}"))]
        fields.extend(flatten_form_fields("PARAMS", params))
        tab_url, tab_html = client.post_form(service_url, fields)
    else:
        tab_url, tab_html = client.fetch(service_url)
    if 'name="form_auth"' in tab_html or not tab_html.strip():
        return {
            "status": "BLOCKED",
            "blockers": ["CRM_COMPANY_INCOME_CONTRACT_TAB_UNREADABLE"],
            "company_card_url": company_url,
            "tab_id": tab_id,
            "tab_url": tab_url,
            "rows": [],
        }

    sort_url = grid_sort_url(tab_html, "Дата заключения")
    date_sort_verified = False
    first_url, first_html = tab_url, tab_html
    if sort_url:
        first_url, first_html = client.fetch(sort_url)
        date_sort_verified = bool(first_html.strip()) and 'name="form_auth"' not in first_html

    rows_by_id: dict[str, dict[str, object]] = {}
    coverage_complete = False
    pages_checked = 0
    for page in range(1, max_pages + 1):
        if page == 1:
            page_url, page_html = first_url, first_html
        else:
            page_url = paged_url(first_url, page)
            _, page_html = client.fetch(page_url)
        pages_checked += 1
        parsed_rows = parse_income_contract_rows(page_html)
        if not parsed_rows:
            coverage_complete = True
            break
        fresh = 0
        for row in parsed_rows:
            row_id = str(row.get("id") or "")
            if not row_id or row_id in rows_by_id:
                continue
            fresh += 1
            row["company_id"] = company_id
            row["company_name"] = company_name
            row["company_field"] = "Карточка компании"
            row["source_tab_id"] = tab_id
            row["source_tab_name"] = tab_name
            row["source_url"] = page_url
            row["source_mode"] = "COMPANY_CARD_INCOME_CONTRACTS_TAB_FALLBACK"
            validity = row.get("validity")
            if isinstance(validity, dict):
                validity["source_url"] = page_url
                validity["source_tab_id"] = tab_id
            rows_by_id[row_id] = row
        if fresh == 0:
            coverage_complete = True
            break

    blockers: list[str] = []
    if not date_sort_verified:
        blockers.append("CRM_COMPANY_INCOME_CONTRACT_DATE_SORT_NOT_VERIFIED")
    if not coverage_complete:
        blockers.append("CRM_COMPANY_INCOME_CONTRACT_TAB_COVERAGE_NOT_VERIFIED")
    if not rows_by_id:
        blockers.append("CRM_COMPANY_INCOME_CONTRACT_ROWS_EMPTY")
    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "blockers": blockers,
        "company_id": company_id,
        "company_name": company_name,
        "company_card_url": company_url,
        "company_card_attempted_urls": attempted_urls,
        "tab_id": tab_id,
        "tab_name": tab_name,
        "tab_url": first_url,
        "date_sort_verified": date_sort_verified,
        "coverage_complete": coverage_complete,
        "pages_checked": pages_checked,
        "rows": list(rows_by_id.values()),
    }


def command_list_income_contracts(
    client: BitrixSessionClient,
    company_name: str,
    max_pages: int,
    output: str,
    company_id: str | None = None,
    company_card_url: str | None = None,
) -> int:
    """Read the canonical list, then the exact company card only on a true miss."""
    client.login_portal()
    initial_url, initial_html = client.fetch(INCOME_CONTRACT_LIST_PATH)
    if 'name="form_auth"' in initial_html:
        raise RuntimeError("Список доходных договоров вернул форму авторизации")

    sort_url = grid_sort_url(initial_html, "Дата заключения")
    date_sort_verified = False
    first_url, first_html = initial_url, initial_html
    if sort_url:
        first_url, first_html = client.fetch(sort_url)
        date_sort_verified = bool(first_html.strip()) and 'name="form_auth"' not in first_html

    needle = normalized_company_name(company_name)
    if not needle:
        raise ValueError("Для списка доходных договоров требуется точное название компании")
    if company_card_url and not company_id:
        raise ValueError("--company-card-url requires --company-id")
    if company_card_url and not is_company_card_url_for_id(
        company_card_url, company_id, client.base_url
    ):
        raise ValueError(
            "--company-card-url must point to the exact --company-id card on this CRM portal"
        )

    rows_by_id: dict[str, dict[str, object]] = {}
    seen_row_ids: set[str] = set()
    coverage_complete = False
    pages_checked = 0
    for page in range(1, max_pages + 1):
        if page == 1:
            page_url, page_html = first_url, first_html
        else:
            page_url = paged_url(first_url or INCOME_CONTRACT_LIST_PATH, page)
            _, page_html = client.fetch(page_url)
        pages_checked += 1
        parsed_rows = parse_income_contract_rows(page_html)
        if not parsed_rows:
            coverage_complete = True
            break
        fresh = 0
        for row in parsed_rows:
            row_id = str(row.get("id") or "")
            if not row_id or row_id in seen_row_ids:
                continue
            seen_row_ids.add(row_id)
            fresh += 1
            row_company_name = re.sub(r"\s+", " ", str(row.get("company_name") or "")).strip()
            if row_company_name and needle == normalized_company_name(row_company_name):
                row["source_list_url"] = INCOME_CONTRACT_LIST_URL
                row["source_mode"] = "CANONICAL_LIST"
                rows_by_id[row_id] = row
        if fresh == 0:
            coverage_complete = True
            break

    canonical_rows = list(rows_by_id.values())
    fallback: dict[str, object] | None = None
    if not canonical_rows and coverage_complete:
        if company_id:
            exact_cards = [
                {
                    "id": str(company_id),
                    "title": re.sub(r"\s+", " ", company_name).strip(),
                    "type": "",
                    "url": company_card_url or f"/crm/company/details/{company_id}/",
                }
            ]
        else:
            exact_cards = [
                row
                for row in collect_company_matches(client, company_name, max_pages)
                if normalized_company_name(row.get("title")) == needle
            ]
        if len(exact_cards) == 1:
            fallback = collect_company_card_income_contracts(
                client,
                exact_cards[0],
                max_pages,
            )
        else:
            fallback = {
                "status": "BLOCKED",
                "blockers": [
                    "CRM_EXACT_COMPANY_CARD_NOT_FOUND"
                    if not exact_cards
                    else "CRM_EXACT_COMPANY_CARD_AMBIGUOUS"
                ],
                "rows": [],
            }

    fallback_pass = bool(fallback and fallback.get("status") == "PASS")
    rows = (
        list(fallback.get("rows") or [])
        if fallback_pass and fallback is not None
        else canonical_rows
    )
    source_mode = (
        "COMPANY_CARD_INCOME_CONTRACTS_TAB_FALLBACK"
        if fallback_pass
        else "CANONICAL_LIST"
    )
    payload = {
        "schema_version": "1.2.0",
        "tab_name": "Доходные договоры",
        "source": {
            "list_url": INCOME_CONTRACT_LIST_URL,
            "mode": source_mode,
            "final_url": (
                fallback.get("tab_url")
                if fallback_pass and fallback is not None
                else first_url
            ),
            "canonical_list_company_found": bool(canonical_rows),
            "canonical_list_coverage_complete": coverage_complete,
            "fallback_attempted": fallback is not None,
            "fallback_status": fallback.get("status") if fallback else None,
            "fallback_reason": (
                "COMPANY_NOT_FOUND_IN_CANONICAL_LIST" if fallback is not None else None
            ),
            "company_id": fallback.get("company_id") if fallback else None,
            "company_card_url": fallback.get("company_card_url") if fallback else None,
            "company_card_attempted_urls": (
                fallback.get("company_card_attempted_urls")
                if fallback
                else []
            ),
            "tab_id": fallback.get("tab_id") if fallback else None,
            "tab_name": fallback.get("tab_name") if fallback else None,
            "tab_url": fallback.get("tab_url") if fallback else None,
            "retrieved_at": dt.date.today().isoformat(),
        },
        "company_filter": company_name,
        "company_match_verified": bool(rows) and all(
            needle == normalized_company_name(row.get("company_name")) for row in rows
        ),
        "selection_hint": INCOME_CONTRACT_CHAIN_SELECTION_HINT,
        "date_sort_verified": (
            bool(fallback.get("date_sort_verified"))
            if fallback_pass and fallback is not None
            else date_sort_verified
        ),
        "coverage_complete": (
            bool(fallback.get("coverage_complete"))
            if fallback_pass and fallback is not None
            else coverage_complete
        ),
        "pages_checked": (
            fallback.get("pages_checked")
            if fallback_pass and fallback is not None
            else pages_checked
        ),
        "fallback_blockers": list(fallback.get("blockers") or []) if fallback else [],
        "rows": rows,
    }
    output_path = pathlib.Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_file(output_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "PASS" if rows and payload["date_sort_verified"] and payload["coverage_complete"] else "BLOCKED",
                "source_list_url": INCOME_CONTRACT_LIST_URL,
                "source_mode": source_mode,
                "company": company_name,
                "rows": len(rows),
                "date_sort_verified": payload["date_sort_verified"],
                "coverage_complete": payload["coverage_complete"],
                "fallback_blockers": payload["fallback_blockers"],
                "output": str(output_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if rows and payload["date_sort_verified"] and payload["coverage_complete"] else 2


def command_download_selected_income_contract(
    client: BitrixSessionClient,
    selection_path: str,
    output_dir: str,
    requested_file_url: str | None = None,
) -> int:
    """Download only the already selected base income-contract file."""

    source = pathlib.Path(selection_path).expanduser().resolve()
    try:
        selection = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Не удалось прочитать selection JSON: {exc}") from exc
    selected = selection.get("selected") if isinstance(selection, dict) else None
    if not isinstance(selected, dict) or selection.get("status") != "PASS":
        raise SystemExit("Скачивание разрешено только для выбранного договора со статусом PASS.")

    # Команда обычно запускается отдельным CLI-процессом после чтения списка.
    # Поэтому CookieJar в ней новый и пустой: до защищённого вложения нужна
    # собственная авторизованная CRM-сессия, а не память предыдущей команды.
    client.login_portal()

    urls = [
        normalize_crm_path(str(value))
        for value in selected.get("contract_file_urls", [])
        if normalize_crm_path(str(value))
    ]
    detail_url = normalize_crm_path(str(selected.get("detail_url") or ""))
    detail_fetch_url = None
    if not urls and detail_url:
        detail_fetch_url, raw_html = client.fetch(detail_url)
        urls = [normalize_crm_path(value) for value in extract_file_links(raw_html)]
        urls = [value for value in urls if value]
    urls = list(dict.fromkeys(urls))

    chosen = normalize_crm_path(requested_file_url or "")
    if chosen:
        if urls and chosen not in urls:
            raise SystemExit("Указанный --file-url отсутствует в выбранной строке/карточке договора.")
    elif len(urls) == 1:
        chosen = urls[0]
    elif not urls:
        raise SystemExit("У выбранного доходного договора не найден файл для точечного скачивания.")
    else:
        raise SystemExit(
            "У выбранного доходного договора найдено несколько файлов; "
            "просмотрите список и повторите с точным --file-url.\n" + "\n".join(urls)
        )

    final_url, body = client.fetch_binary(chosen)
    extension = guess_extension(body, pathlib.Path(urllib.parse.urlparse(final_url).path).suffix or ".bin")
    filename = safe_filename_from_url(final_url, f"income-contract-{selected.get('id')}{extension}")
    if pathlib.Path(filename).suffix.casefold() not in {".docx", ".pdf", ".doc"}:
        filename = f"income-contract-{selected.get('id')}{extension}"
    target_dir = pathlib.Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    save_binary_file(target, body)
    manifest = {
        "schema_version": "1.0.0",
        "status": "PASS",
        "selection_path": str(source),
        "selection_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "selected_contract_id": selected.get("id"),
        "selected_detail_url": detail_url,
        "detail_fetch_url": detail_fetch_url,
        "source_file_url": chosen,
        "final_file_url": final_url,
        "file": str(target),
        "file_sha256": hashlib.sha256(body).hexdigest(),
        "download_scope": "SELECTED_INCOME_CONTRACT_ONLY",
    }
    manifest_path = target_dir / "income-contract-download.json"
    write_text_file(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def write_text_file(path: pathlib.Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def save_binary_file(path: pathlib.Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def safe_filename_from_url(url: str, fallback: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = pathlib.Path(parsed.path).name or fallback
    name = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ._-]+", "_", name)
    return name or fallback


def collect_company_matches(client: BitrixSessionClient, company_name: str, max_pages: int) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    needle = company_name.lower()
    seen_ids: set[str] = set()
    for page in range(1, max_pages + 1):
        try:
            _, page_html = client.fetch(f"/crm/company/list/?page={page}")
        except Exception:
            break
        rows = parse_company_rows(page_html)
        if not rows:
            break
        fresh_rows = [row for row in rows if row["id"] not in seen_ids]
        if not fresh_rows:
            break
        for row in fresh_rows:
            seen_ids.add(row["id"])
            if needle in row["title"].lower():
                matches.append(row)
    return matches


def collect_deal_matches(client: BitrixSessionClient, company_name: str, max_pages: int) -> list[dict[str, str]]:
    try:
        _, first_page_html = client.fetch("/crm/deal/list/")
    except Exception:
        return []
    bxajaxid = extract_bxajaxid(first_page_html)
    matches: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    needle = company_name.lower()
    for page in range(1, max_pages + 1):
        try:
            _, page_html = client.fetch_deal_grid_page(page, bxajaxid)
        except Exception:
            break
        rows = parse_deal_rows(page_html)
        if not rows:
            break
        fresh_rows = [row for row in rows if row["id"] not in seen_ids]
        if not fresh_rows:
            break
        for row in fresh_rows:
            seen_ids.add(row["id"])
            if needle in row["company"].lower():
                row["source"] = "deal_grid"
                matches.append(row)
    return matches


def validate_dossier_output_dir(output_dir: str) -> pathlib.Path:
    """Return an explicit dossier root only when it is outside every skill folder.

    CRM snapshots are run-specific evidence, not skill files. Writing them below a
    ``skills`` directory corrupts a packaged skill's integrity check and risks
    carrying one case into the next. Keep this guard in the bridge itself so it
    also protects callers that do not follow the master-skill instructions.
    """
    root_dir = pathlib.Path(output_dir).expanduser().resolve()
    if any(parent.name == "skills" for parent in (root_dir, *root_dir.parents)):
        raise ValueError(
            "CRM dossier нельзя записывать внутри папки skills; "
            "укажите отдельную папку запуска через --output-dir."
        )
    return root_dir


def dossier_directory(output_dir: str, company_name: str, company_id: str | None) -> pathlib.Path:
    root_dir = ensure_dir(validate_dossier_output_dir(output_dir))
    company_key = company_name if company_name else f"company-{company_id}"
    return ensure_dir(root_dir / slugify(company_key))


def preserve_last_successful_dossier(company_dir: pathlib.Path) -> None:
    """Keep a recoverable copy before a later run replaces a successful dossier."""
    report_path = company_dir / "metadata" / "run_report.json"
    if not report_path.is_file():
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if report.get("status") not in {"ok", "success", "completed"}:
        return
    backup_dir = ensure_dir(company_dir / "metadata" / "last-successful")
    for relative_name in CORE_DOSSIER_FILES:
        source = company_dir / relative_name
        if source.is_file():
            shutil.copy2(source, backup_dir / relative_name.replace("/", "__"))


def write_initial_dossier_artifacts(
    company_dir: pathlib.Path,
    company_name: str,
    company_id: str | None,
    mode: str,
) -> None:
    """Write the minimum, honest dossier state before network traversal begins."""
    meta_dir = ensure_dir(company_dir / "metadata")
    run_report = {
        "started_at": now_iso(),
        "finished_at": "",
        "mode": mode,
        "company_name": company_name,
        "company_id": company_id or "",
        "status": "running",
        "summary": {},
        "checks": [],
        "errors": [],
        "warnings": [],
    }
    write_text_file(meta_dir / "run_report.json", json.dumps(run_report, ensure_ascii=False, indent=2))
    write_text_file(meta_dir / "lazy_tabs.json", "[]\n")
    write_text_file(meta_dir / "documents.json", "[]\n")
    write_text_file(
        company_dir / "context.md",
        "# CRM dossier\n\nСбор CRM-контекста выполняется. Итоговый статус см. в `metadata/run_report.json`.\n",
    )


def write_failed_dossier_artifacts(
    output_dir: str,
    company_name: str,
    company_id: str | None,
    mode: str,
    exc: Exception,
) -> None:
    """Leave an explicit terminal diagnostic if collection exits unexpectedly."""
    company_dir = dossier_directory(output_dir, company_name, company_id)
    meta_dir = ensure_dir(company_dir / "metadata")
    for file_name in ("lazy_tabs.json", "documents.json"):
        path = meta_dir / file_name
        if not path.is_file():
            write_text_file(path, "[]\n")
    report = {
        "started_at": "",
        "finished_at": now_iso(),
        "mode": mode,
        "company_name": company_name,
        "company_id": company_id or "",
        "status": "failed",
        "summary": {},
        "checks": [],
        "errors": [{"time": now_iso(), "kind": "unhandled_collection_error", "target": "build-company-dossier", "error": error_text(exc)}],
        "warnings": ["Сбор остановился до финализации; raw-снимки не являются готовым CRM dossier."],
    }
    write_text_file(meta_dir / "run_report.json", json.dumps(report, ensure_ascii=False, indent=2))
    write_text_file(
        company_dir / "context.md",
        "# CRM dossier\n\nСбор CRM-контекста завершился ошибкой. Использование dossier для preflight запрещено; причина зафиксирована в `metadata/run_report.json`.\n",
    )


def command_collect_company_context(
    client: BitrixSessionClient,
    company_name: str,
    company_id: str | None,
    output_dir: str | None,
    max_company_pages: int,
    max_deal_pages: int,
    mode: str,
    skip_document_downloads: bool = False,
    max_related_cards: int = DEFAULT_MAX_RELATED_CARDS,
) -> int:
    company_key = company_name if company_name else f"company-{company_id}"
    company_dir = dossier_directory(output_dir, company_name, company_id)
    raw_dir = ensure_dir(company_dir / "raw")
    docs_dir = ensure_dir(company_dir / "documents")
    meta_dir = ensure_dir(company_dir / "metadata")
    disk_dir = ensure_dir(docs_dir / "bitrix_disk")
    crm_files_dir = ensure_dir(docs_dir / "crm_fields")
    preserve_last_successful_dossier(company_dir)
    for file_name in GENERATED_METADATA_FILES:
        stale_path = meta_dir / file_name
        if stale_path.exists():
            stale_path.unlink()

    company_matches: list[dict[str, str]] = []
    deal_matches: list[dict[str, str]] = []
    company_details: list[dict[str, object]] = []
    deal_details: list[dict[str, object]] = []
    downloaded_docs: list[dict[str, str]] = []
    entity_links: list[dict[str, str]] = []
    saved_pages: list[dict[str, str]] = []
    related_entities: list[dict[str, str]] = []
    tab_inventory: list[dict[str, object]] = []
    lazy_tab_pages: list[dict[str, str]] = []
    timeline_highlights: list[dict[str, str]] = []
    communications: list[dict[str, str]] = []
    income_contract_rows: list[dict[str, object]] = []
    document_entrypoints: dict[str, str] = {}
    related_detail_links: list[str] = []
    document_download_limit = env_int("B24_MAX_DOCUMENT_DOWNLOADS", DEFAULT_MAX_DOCUMENT_DOWNLOADS)
    document_download_limit_warned = False

    run_report: dict[str, object] = {
        "started_at": now_iso(),
        "finished_at": "",
        "mode": mode,
        "document_downloads_skipped": skip_document_downloads,
        "company_name": company_name,
        "company_id": company_id or "",
        "status": "running",
        "summary": {},
        "checks": [],
        "errors": [],
        "warnings": [],
    }
    write_initial_dossier_artifacts(company_dir, company_name, company_id, mode)

    def add_check(kind: str, target: str, status: str, **extra: object) -> None:
        item: dict[str, object] = {
            "time": now_iso(),
            "kind": kind,
            "target": target,
            "status": status,
        }
        item.update(extra)
        checks = run_report["checks"]
        assert isinstance(checks, list)
        checks.append(item)

    def add_error(kind: str, target: str, exc: Exception) -> None:
        item = {"time": now_iso(), "kind": kind, "target": target, "error": error_text(exc)}
        errors = run_report["errors"]
        assert isinstance(errors, list)
        errors.append(item)
        add_check(kind, target, "error", error=item["error"])

    def add_warning(message: str, **extra: object) -> None:
        item: dict[str, object] = {"time": now_iso(), "message": message}
        item.update(extra)
        warnings = run_report["warnings"]
        assert isinstance(warnings, list)
        warnings.append(item)

    def save_run_report(status: str) -> None:
        errors = run_report["errors"]
        warnings = run_report["warnings"]
        assert isinstance(errors, list)
        assert isinstance(warnings, list)
        run_report["finished_at"] = now_iso()
        run_report["status"] = status
        run_report["summary"] = {
            "company_cards": len(company_matches),
            "deals": len(deal_matches),
            "saved_pages": len(saved_pages),
            "related_entities": len(related_entities),
            "tabs": len(tab_inventory),
            "lazy_tabs": len(lazy_tab_pages),
            "documents": len(downloaded_docs),
            "entity_links": len(entity_links),
            "income_contract_rows": len(income_contract_rows),
            "errors": len(errors),
            "warnings": len(warnings),
        }
        write_text_file(meta_dir / "run_report.json", json.dumps(run_report, ensure_ascii=False, indent=2))

    try:
        final_login_url = client.login_portal()
        add_check("login", final_login_url, "ok")
    except Exception as exc:
        add_error("login", client.base_url, exc)
        save_run_report("failed")
        raise

    company_matches.extend(
        [{"id": company_id, "title": company_name or f"Компания {company_id}", "type": "", "url": f"/crm/company/details/{company_id}/"}]
        if company_id
        else collect_company_matches(client, company_name, max_company_pages)
    )
    if company_name and mode != "package":
        for deal in collect_deal_matches(client, company_name, max_deal_pages):
            merge_deal_match(deal_matches, deal)
            merge_entity_ref(
                entity_links,
                {
                    "kind": "deal",
                    "entity_type_id": "2",
                    "id": deal.get("id", ""),
                    "title": deal.get("title", ""),
                    "url": deal.get("url", ""),
                    "source": deal.get("source", "deal_grid"),
                },
            )
    if not company_matches:
        add_warning("Карточки компаний не найдены", company_name=company_name)

    def save_downloaded_binary(
        content: bytes,
        target_dir: pathlib.Path,
        requested_name: str,
        fallback_name: str,
    ) -> str:
        file_name = safe_filename_from_url(requested_name, fallback_name)
        if "." not in pathlib.Path(file_name).name:
            file_name += guess_extension(content)
        file_path = target_dir / file_name
        if file_path.exists():
            return str(file_path.relative_to(company_dir))
        save_binary_file(file_path, content)
        return str(file_path.relative_to(company_dir))

    def download_url(source_path: str, url: str, target_dir: pathlib.Path, name: str, kind: str) -> None:
        nonlocal document_download_limit_warned
        is_priority_crm_field = kind == "crm_field"
        if len(downloaded_docs) >= document_download_limit and not is_priority_crm_field:
            if not document_download_limit_warned:
                add_warning("Достигнут лимит скачивания документов", limit=document_download_limit)
                document_download_limit_warned = True
            return
        cached_name = safe_filename_from_url(name, "file")
        cached_path = target_dir / cached_name
        if cached_path.exists():
            file_rel_path = str(cached_path.relative_to(company_dir))
            downloaded_docs.append(
                {
                    "kind": kind,
                    "source": source_path,
                    "url": url,
                    "file": file_rel_path,
                    "size": str(cached_path.stat().st_size),
                    "status": "cached",
                }
            )
            add_check("download", url, "cached", file=file_rel_path, size=cached_path.stat().st_size)
            return
        try:
            file_url, file_body = client.fetch_binary(url)
            file_rel_path = save_downloaded_binary(file_body, target_dir, name, "file")
            downloaded_docs.append(
                {
                    "kind": kind,
                    "source": source_path,
                    "url": file_url,
                    "file": file_rel_path,
                    "size": str(len(file_body)),
                }
            )
            add_check("download", url, "ok", file=file_rel_path, size=len(file_body))
        except Exception as exc:
            add_error("download", url, exc)
            return

    def download_shared_disk_folder(source_path: str, folder_link: str) -> None:
        try:
            folder_url, folder_html = client.fetch(folder_link)
        except Exception as exc:
            add_error("disk_folder", folder_link, exc)
            return
        folder_slug = slugify(urllib.parse.unquote(urllib.parse.urlparse(folder_url).path).rstrip("/").split("/")[-1])
        folder_raw_path = raw_dir / f"disk_folder_{folder_slug}.html"
        write_text_file(folder_raw_path, folder_html)
        add_check("disk_folder", folder_link, "ok", final_url=folder_url, html=folder_raw_path.name)
        for item in extract_disk_downloads(folder_html):
            download_url(folder_link, item["url"], disk_dir / folder_slug, item["name"], "bitrix_disk")

    def download_documents_from_html(source_path: str, raw_html: str) -> None:
        nonlocal document_download_limit_warned
        if skip_document_downloads:
            return
        for ref in extract_crm_item_file_refs(raw_html):
            name = f"{ref['field_name']}_{ref['file_id']}"
            download_url(source_path, ref["url"], crm_files_dir, name, "crm_field")
        for link in extract_file_links(raw_html):
            if len(downloaded_docs) >= document_download_limit:
                if not document_download_limit_warned:
                    add_warning("Достигнут лимит скачивания документов", limit=document_download_limit)
                    document_download_limit_warned = True
                return
            cached_name = safe_filename_from_url(link, "file")
            cached_path = docs_dir / cached_name
            if cached_path.exists():
                downloaded_docs.append(
                    {
                        "source": source_path,
                        "url": link,
                        "file": f"documents/{cached_name}",
                        "size": str(cached_path.stat().st_size),
                        "status": "cached",
                    }
                )
                add_check("download", link, "cached", file=f"documents/{cached_name}", size=cached_path.stat().st_size)
                continue
            try:
                file_url, file_body = client.fetch_binary(link)
                file_name = safe_filename_from_url(file_url, "file")
                file_path = docs_dir / file_name
                if not file_path.exists():
                    save_binary_file(file_path, file_body)
                downloaded_docs.append({"source": source_path, "url": file_url, "file": f"documents/{file_name}"})
                add_check("download", link, "ok", file=f"documents/{file_name}", size=len(file_body))
            except Exception as exc:
                add_error("download", link, exc)
                continue
        for folder_link in extract_shared_disk_folder_links(raw_html):
            download_shared_disk_folder(source_path, folder_link)

    def record_entity_refs(source_path: str, raw_html: str, include_deals: bool = True) -> None:
        refs = extract_entity_refs(raw_html, source_path)
        for ref in refs:
            if ref.get("kind") == "deal":
                if not include_deals:
                    continue
                merge_entity_ref(entity_links, ref)
                merge_deal_match(
                    deal_matches,
                    {
                        "id": ref.get("id", ""),
                        "title": ref.get("title", ""),
                        "company": company_name,
                        "stage": "",
                        "responsible": "",
                        "amount": "",
                        "date_create": "",
                        "contact": "",
                        "url": ref.get("url", ""),
                        "source": ref.get("source", "entity_link"),
                    },
                )
                continue
            merge_entity_ref(entity_links, ref)
            if ref.get("kind") in {"dynamic", "smart_invoice", "quote"}:
                url = ref.get("url", "")
                if url and url not in related_detail_links:
                    related_detail_links.append(url)

    def record_detail_data(source_kind: str, source_id: str, source_path: str, raw_html: str) -> None:
        for item in extract_entity_data_objects(raw_html):
            entity_type_id = str(item.get("entity_type_id", ""))
            data = item.get("data")
            if not isinstance(data, dict):
                continue
            item_id = str(data.get("ID", ""))
            if entity_type_id == "4" and item_id == source_id:
                company_details.append({"source": source_path, "entity_type_id": entity_type_id, "data": data})
            elif entity_type_id == "2":
                deal_id = item_id or source_id
                if not deal_id or deal_id == "0":
                    continue
                deal_details.append({"source": source_path, "entity_type_id": entity_type_id, "data": data})
                merge_deal_match(
                    deal_matches,
                    {
                        "id": deal_id,
                        "title": value_from_signed_field(data.get("TITLE")),
                        "company": value_from_signed_field(data.get("COMPANY_TITLE")) or company_name,
                        "stage": "",
                        "responsible": "",
                        "amount": "",
                        "date_create": "",
                        "contact": "",
                        "url": f"/crm/deal/details/{deal_id}/",
                        "source": source_path,
                    },
                )
                for deal in deal_matches:
                    if deal.get("id") != deal_id:
                        continue
                    enrich_deal_from_detail_data(deal, data)
                    break
            elif entity_type_id and item_id:
                url = f"/crm/type/{entity_type_id}/details/{item_id}/"
                if CRM_ENTITY_TYPES.get(entity_type_id) is None:
                    ref = {
                        "kind": "dynamic",
                        "entity_type_id": entity_type_id,
                        "id": item_id,
                        "title": value_from_signed_field(data.get("TITLE")),
                        "url": url,
                        "source": source_path,
                    }
                    merge_entity_ref(entity_links, ref)
                    if source_kind != "related" and url not in related_detail_links:
                        related_detail_links.append(url)

    def record_tabs(source_kind: str, source_id: str, source_path: str, raw_html: str) -> list[dict[str, object]]:
        tabs = extract_tab_loaders(raw_html)
        for tab in tabs:
            tab_record = {
                "source_kind": source_kind,
                "source_id": source_id,
                "source_path": source_path,
                **tab,
            }
            tab_inventory.append(tab_record)
        return tabs

    def collect_lazy_tabs(source_kind: str, source_id: str, source_path: str, raw_html: str) -> None:
        tabs = record_tabs(source_kind, source_id, source_path, raw_html)
        for tab in tabs:
            tab_id = str(tab.get("id", ""))
            if source_kind == "company" and tab_id in COMPANY_TAB_COLLECT_DENYLIST:
                continue
            service_url = str(tab.get("service_url", ""))
            if not service_url:
                continue
            try:
                component_data = tab.get("component_data")
                if isinstance(component_data, dict) and component_data:
                    params = dict(component_data)
                    params["TAB_ID"] = tab_id
                    fields = [("LOADER_ID", slugify(f"{source_kind}-{source_id}-{tab_id}"))]
                    fields.extend(flatten_form_fields("PARAMS", params))
                    final_url, tab_html = client.post_form(service_url, fields)
                else:
                    final_url, tab_html = client.fetch(service_url)
            except Exception as exc:
                add_error("lazy_tab", service_url, exc)
                continue
            if 'name="form_auth"' in tab_html:
                add_warning("Ленивая вкладка вернула форму авторизации", source_kind=source_kind, source_id=source_id, tab_id=tab_id)
                continue
            if not tab_html.strip():
                add_warning("Ленивая вкладка вернула пустой ответ", source_kind=source_kind, source_id=source_id, tab_id=tab_id)
                continue
            tab_slug = slugify(f"{source_kind}-{source_id}-{tab_id}")
            html_path = raw_dir / f"tab_{tab_slug}.html"
            text_path = raw_dir / f"tab_{tab_slug}.txt"
            write_text_file(html_path, tab_html)
            write_text_file(text_path, clean_text(tab_html))
            add_check("lazy_tab", service_url, "ok", source_kind=source_kind, source_id=source_id, tab_id=tab_id, html=html_path.name)
            lazy_tab_pages.append(
                {
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "tab_id": tab_id,
                    "tab_name": str(tab.get("name", "")),
                    "url": final_url,
                    "html": html_path.name,
                    "text": text_path.name,
                }
            )
            if source_kind == "company" and (
                tab_id == "tab_relation_dynamic_142"
                or "entityTypeId=142" in service_url
            ):
                income_html = tab_html
                income_source_url = final_url
                sort_url = grid_sort_url(tab_html, "Дата заключения")
                date_sort_verified = False
                if sort_url:
                    try:
                        sorted_url, sorted_html = client.fetch(sort_url)
                        if sorted_html.strip() and 'name="form_auth"' not in sorted_html:
                            income_html = sorted_html
                            income_source_url = sorted_url
                            date_sort_verified = True
                            sorted_html_path = raw_dir / f"tab_{tab_slug}-date-ascending.html"
                            sorted_text_path = raw_dir / f"tab_{tab_slug}-date-ascending.txt"
                            write_text_file(sorted_html_path, sorted_html)
                            write_text_file(sorted_text_path, clean_text(sorted_html))
                            add_check(
                                "income_contract_tab_sorted",
                                sort_url,
                                "ok",
                                source_kind=source_kind,
                                source_id=source_id,
                                html=sorted_html_path.name,
                            )
                    except Exception as exc:
                        add_error("income_contract_tab_sort", sort_url, exc)
                parsed_income_rows = parse_income_contract_rows(income_html)
                for row in parsed_income_rows:
                    row.update(
                        {
                            "company_id": source_id,
                            "source_tab_id": tab_id,
                            "source_url": income_source_url,
                            "date_sort_requested": bool(sort_url),
                            "date_sort_verified": date_sort_verified,
                        }
                    )
                    income_contract_rows.append(row)
            for link in extract_redirect_links(tab_html):
                ref = classify_entity_path(link)
                if not ref or ref.get("kind") == "deal":
                    continue
                if link not in related_detail_links:
                    related_detail_links.append(link)
            if tab_id == "tab_deal" or "crm.deal.list" in service_url:
                for deal in parse_deal_rows(tab_html):
                    deal["source"] = f"tab:{tab_id}"
                    merge_deal_match(deal_matches, deal)
                    merge_entity_ref(
                        entity_links,
                        {
                            "kind": "deal",
                            "entity_type_id": "2",
                            "id": deal.get("id", ""),
                            "title": deal.get("title", ""),
                            "url": deal.get("url", ""),
                            "source": f"tab:{tab_id}",
                        },
                    )
            record_entity_refs(service_url, tab_html, include_deals=False)
            record_detail_data("tab", tab_id, service_url, tab_html)
            download_documents_from_html(service_url, tab_html)

    for company in company_matches:
        detail_path = make_iframe_path(company["url"])
        try:
            final_url, html_body = client.fetch(detail_path)
        except Exception as exc:
            add_error("company_card", detail_path, exc)
            continue
        text_body = clean_text(html_body)
        html_path = raw_dir / f'company_{company["id"]}.html'
        text_path = raw_dir / f'company_{company["id"]}.txt'
        write_text_file(html_path, html_body)
        write_text_file(text_path, text_body)
        saved_pages.append({"kind": "company", "id": company["id"], "url": final_url, "html": html_path.name, "text": text_path.name})
        add_check("company_card", detail_path, "ok", final_url=final_url, html=html_path.name)
        record_detail_data("company", company["id"], detail_path, html_body)
        record_entity_refs(detail_path, html_body)
        if mode in {"full", "deep"}:
            collect_lazy_tabs("company", company["id"], detail_path, html_body)
        else:
            record_tabs("company", company["id"], detail_path, html_body)
        timeline_highlights.extend(extract_timeline_highlights(html_body))
        communications.extend(extract_contact_communications(html_body))
        related_detail_links.extend(extract_redirect_links(html_body))
        document_entrypoints.update(extract_document_generator_urls(html_body))
        download_documents_from_html(detail_path, html_body)

    title_by_related_link = {
        item["link"]: item["link_text"]
        for item in timeline_highlights
        if item.get("link") and item.get("link_text")
    }
    if mode != "package":
        for deal in collect_deal_matches_from_links(related_detail_links, title_by_related_link, company_name):
            merge_deal_match(deal_matches, deal)

    for deal in deal_matches:
        detail_path = make_iframe_path(deal["url"])
        try:
            final_url, html_body = client.fetch(detail_path)
        except Exception as exc:
            add_error("deal_card", detail_path, exc)
            continue
        text_body = clean_text(html_body)
        html_path = raw_dir / f'deal_{deal["id"]}.html'
        text_path = raw_dir / f'deal_{deal["id"]}.txt'
        write_text_file(html_path, html_body)
        write_text_file(text_path, text_body)
        saved_pages.append({"kind": "deal", "id": deal["id"], "url": final_url, "html": html_path.name, "text": text_path.name})
        add_check("deal_card", detail_path, "ok", final_url=final_url, html=html_path.name)
        record_detail_data("deal", deal["id"], detail_path, html_body)
        record_entity_refs(detail_path, html_body, include_deals=False)
        if mode in {"full", "deep"}:
            collect_lazy_tabs("deal", deal["id"], detail_path, html_body)
        else:
            record_tabs("deal", deal["id"], detail_path, html_body)
        download_documents_from_html(detail_path, html_body)

    unique_links: list[str] = []
    seen_links: set[str] = set()
    for link in related_detail_links:
        if link in seen_links:
            continue
        seen_links.add(link)
        unique_links.append(link)
    related_detail_links = [link for link in unique_links if not extract_deal_id_from_path(link)]

    if mode in {"quick", "package"}:
        related_detail_links = []

    for index, link in enumerate(related_detail_links, start=1):
        if index > max_related_cards:
            add_warning(
                "Достигнут лимит связанных CRM-карточек; dossier завершён с ограниченным охватом.",
                limit=max_related_cards,
                skipped=len(related_detail_links) - max_related_cards,
            )
            break
        try:
            fetch_path = make_iframe_path(link)
            final_url, html_body = client.fetch(fetch_path)
            text_body = clean_text(html_body)
            title = extract_title(html_body) or f"Связанная сущность {index}"
            slug = safe_filename_from_url(link, f"related_{index}")
            html_path = raw_dir / f"related_{index:02d}_{slug}.html"
            text_path = raw_dir / f"related_{index:02d}_{slug}.txt"
            write_text_file(html_path, html_body)
            write_text_file(text_path, text_body)
            related_entities.append(
                {
                    "title": title,
                    "path": link,
                    "final_url": final_url,
                    "html": html_path.name,
                    "text": text_path.name,
                }
            )
            add_check("related_card", link, "ok", final_url=final_url, html=html_path.name)
            record_detail_data("related", str(index), link, html_body)
            record_entity_refs(link, html_body, include_deals=False)
            timeline_highlights.extend(extract_timeline_highlights(html_body))
            communications.extend(extract_contact_communications(html_body))
            document_entrypoints.update(extract_document_generator_urls(html_body))
            if mode == "deep":
                collect_lazy_tabs("related", str(index), link, html_body)
            download_documents_from_html(link, html_body)
        except Exception as exc:
            add_error("related_card", link, exc)
            continue

    unique_communications: list[dict[str, str]] = []
    seen_comm_keys: set[tuple[str, str, str]] = set()
    for item in communications:
        key = (item["contact_id"], item["type"], item["value"])
        if key in seen_comm_keys:
            continue
        seen_comm_keys.add(key)
        unique_communications.append(item)
    communications = unique_communications

    unique_timeline_highlights: list[dict[str, str]] = []
    seen_timeline_keys: set[tuple[str, str, str, str, str]] = set()
    for item in timeline_highlights:
        key = (
            item.get("date", ""),
            item.get("header", ""),
            item.get("section", ""),
            item.get("link_text", ""),
            item.get("link", ""),
        )
        if key in seen_timeline_keys:
            continue
        seen_timeline_keys.add(key)
        unique_timeline_highlights.append(item)
    timeline_highlights = unique_timeline_highlights

    if company_details:
        write_text_file(meta_dir / "company_details.json", json.dumps(company_details, ensure_ascii=False, indent=2))
    if deal_details:
        write_text_file(meta_dir / "deal_details.json", json.dumps(deal_details, ensure_ascii=False, indent=2))
    if deal_matches:
        write_text_file(meta_dir / "deal_matches.json", json.dumps(deal_matches, ensure_ascii=False, indent=2))
    if entity_links:
        write_text_file(meta_dir / "entity_links.json", json.dumps(entity_links, ensure_ascii=False, indent=2))
    if income_contract_rows:
        deduplicated_income_rows = {
            str(item.get("id") or ""): item
            for item in income_contract_rows
            if item.get("id")
        }
        income_contract_rows = list(deduplicated_income_rows.values())
        write_text_file(
            meta_dir / "income_contracts.json",
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "tab_name": "Доходные договоры",
                    "selection_hint": INCOME_CONTRACT_CHAIN_SELECTION_HINT,
                    "date_sort_verified": any(
                        bool(item.get("date_sort_verified"))
                        for item in income_contract_rows
                    ),
                    "rows": income_contract_rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    if communications:
        save_tsv(meta_dir / "communications.tsv", communications, ["contact_id", "address_id", "type", "value_type", "value"])
    if timeline_highlights:
        write_text_file(meta_dir / "timeline_highlights.json", json.dumps(timeline_highlights, ensure_ascii=False, indent=2))
    if tab_inventory:
        write_text_file(meta_dir / "tabs.json", json.dumps(tab_inventory, ensure_ascii=False, indent=2))
    if related_entities:
        write_text_file(meta_dir / "related_entities.json", json.dumps(related_entities, ensure_ascii=False, indent=2))
    write_text_file(meta_dir / "lazy_tabs.json", json.dumps(lazy_tab_pages, ensure_ascii=False, indent=2))
    write_text_file(meta_dir / "documents.json", json.dumps(downloaded_docs, ensure_ascii=False, indent=2))
    if not saved_pages:
        add_warning("Не сохранено ни одной CRM-страницы")
    if company_matches and not any(page["kind"] == "company" for page in saved_pages):
        add_warning("Карточки компаний найдены, но не удалось сохранить ни одну карточку компании")
    if deal_matches and not any(page["kind"] == "deal" for page in saved_pages):
        add_warning("Сделки найдены, но не удалось сохранить ни одну карточку сделки")
    if company_id and not any(page["kind"] == "company" for page in saved_pages):
        add_warning("Передан точный company_id, но карточка компании не сохранена", company_id=company_id)
    if company_id and not deal_matches and not entity_links and not related_entities:
        add_warning("Передан точный company_id, но сделки и связанные CRM-сущности не обнаружены", company_id=company_id)

    company_data: dict[str, object] = {}
    for detail in reversed(company_details):
        data = detail.get("data")
        if isinstance(data, dict):
            company_data = data
            break

    def crm_value(data: dict[str, object], *keys: str) -> str:
        for key in keys:
            value = strip_bbcode(value_from_signed_field(data.get(key)))
            if value:
                return value
        return ""

    def truncate_text(value: str, limit: int = 1200) -> str:
        if len(value) <= limit:
            return value
        return value[:limit].rstrip() + "..."

    lines: list[str] = []
    lines.append(f"# {company_key}")
    lines.append("")
    lines.append("## Основной контекст CRM")
    lines.append("")
    if company_name:
        lines.append(f"- Запрошенное название компании: `{company_name}`")
    if company_id:
        lines.append(f"- Запрошенный `company_id`: `{company_id}`")
    lines.append(f"- Найдено карточек компаний: `{len(company_matches)}`")
    lines.append(f"- Найдено сделок: `{len(deal_matches)}`")
    lines.append(f"- Найдено связанных карточек по истории: `{len(related_entities)}`")
    lines.append(f"- Найдено коммуникаций контактов: `{len(communications)}`")
    lines.append(f"- Найдено вкладок в карточке: `{len(tab_inventory)}`")
    lines.append(f"- Скачано документов: `{len(downloaded_docs)}`")
    lines.append("")
    lines.append("## Карточка компании: ключевые поля")
    lines.append("")
    if company_data:
        company_fields = [
            ("Название в CRM", ("TITLE",)),
            ("Юридическое название", ("UF_CRM_1663658158",)),
            ("Ответственный", ("ASSIGNED_BY_FORMATTED_NAME",)),
            ("Дата создания", ("DATE_CREATE",)),
            ("Дата изменения", ("DATE_MODIFY",)),
            ("Сайт / публичная ссылка", ("WEB",)),
            ("E-mail из карточки", ("EMAIL",)),
        ]
        for label, keys in company_fields:
            value = crm_value(company_data, *keys)
            if value:
                lines.append(f"- {label}: {value}")
        comments = crm_value(company_data, "COMMENTS")
        if comments:
            lines.append(f"- Комментарии CRM: {truncate_text(comments)}")
        source = company_details[-1].get("source", "") if company_details else ""
        if source:
            lines.append(f"- Источник данных карточки: `{source}`")
    else:
        lines.append("- Детальные поля карточки компании не были извлечены из HTML-модели CRM.")
    lines.append("")
    lines.append("## Найденные карточки компаний")
    lines.append("")
    if company_matches:
        for company in company_matches:
            lines.append(f"- `{company['id']}` — {company['title']} — {company['type']} — {company['url']}")
    else:
        lines.append("- В просмотренных страницах списка компаний совпадения не найдены.")
    lines.append("")
    lines.append("## Найденные сделки")
    lines.append("")
    if deal_matches:
        for deal in deal_matches:
            source = deal.get("source", "")
            source_note = f" — источник: {source}" if source else ""
            parts = [
                deal.get("company", ""),
                deal.get("title", ""),
                deal.get("stage", ""),
                deal.get("amount", ""),
                deal.get("date_create", ""),
            ]
            if deal.get("contact"):
                parts.append(f"контакт: {deal['contact']}")
            if deal.get("responsible"):
                parts.append(f"ответственный: {deal['responsible']}")
            if deal.get("url"):
                parts.append(deal["url"])
            line_body = " — ".join(part for part in parts if part)
            lines.append(f"- `{deal['id']}` — {line_body}{source_note}")
            if deal.get("comments"):
                lines.append(f"  Комментарий сделки: {truncate_text(deal['comments'], 700)}")
    else:
        lines.append("- В просмотренных страницах списка сделок совпадения не найдены.")
    lines.append("")
    lines.append("## Доступные разделы CRM")
    lines.append("")
    if tab_inventory:
        seen_tab_names: set[str] = set()
        for tab in tab_inventory:
            tab_name = str(tab.get("name", ""))
            if not tab_name or tab_name in seen_tab_names:
                continue
            seen_tab_names.add(tab_name)
            lines.append(f"- {tab_name}")
    else:
        lines.append("- Вкладки карточки не были распознаны.")
    lines.append("")
    lines.append("## Подсветка из истории и timeline")
    lines.append("")
    if timeline_highlights:
        timeline_display_limit = 80
        for item in timeline_highlights[:timeline_display_limit]:
            lines.append(f"- `{item['date']}` — {item['header']} — {item['section']} — {item['link_text']} — {item['link']}")
        if len(timeline_highlights) > timeline_display_limit:
            lines.append(
                f"- Показаны первые {timeline_display_limit} событий из `{len(timeline_highlights)}`; полный список: "
                "[metadata/timeline_highlights.json](metadata/timeline_highlights.json)."
            )
    else:
        lines.append("- В загруженном timeline не удалось выделить связанные элементы с прямыми ссылками.")
    lines.append("")
    lines.append("## Связанные карточки, найденные через историю")
    lines.append("")
    if related_entities:
        for item in related_entities:
            lines.append(f"- {item['title']} — {item['path']} — [html](raw/{item['html']}) — [text](raw/{item['text']})")
    else:
        lines.append("- Через историю и встроенные redirect-ссылки связанные карточки не были собраны.")
    lines.append("")
    lines.append("## Все найденные CRM-сущности и ссылки")
    lines.append("")
    if entity_links:
        for ref in entity_links[:80]:
            title = ref.get("title", "")
            title_note = f" — {title}" if title else ""
            lines.append(
                f"- `{ref.get('kind', '')}` type `{ref.get('entity_type_id', '')}` id `{ref.get('id', '')}`"
                f"{title_note} — {ref.get('url', '')} — источник: {describe_source(ref.get('source', ''))}"
            )
        if len(entity_links) > 80:
            lines.append(f"- Показаны первые 80 ссылок из `{len(entity_links)}`; полный список: [metadata/entity_links.json](metadata/entity_links.json).")
    else:
        lines.append("- Ссылки на сделки, договоры, ДС, заявки и другие CRM-сущности не были извлечены.")
    lines.append("")
    lines.append("## Коммуникации и контакты")
    lines.append("")
    if communications:
        email_count = sum(1 for item in communications if item["type"] == "EMAIL")
        phone_count = sum(1 for item in communications if item["type"] == "PHONE")
        unique_contact_count = len({item["contact_id"] for item in communications})
        lines.append(f"- Уникальных контактов в коммуникациях: `{unique_contact_count}`")
        lines.append(f"- E-mail адресов: `{email_count}`")
        lines.append(f"- Телефонных номеров: `{phone_count}`")
        lines.append(f"- Полный список: [metadata/communications.tsv](metadata/communications.tsv)")
    else:
        lines.append("- Коммуникации контактов в карточке не были найдены.")
    lines.append("")
    lines.append("## Архивные снимки CRM")
    lines.append("")
    for page in saved_pages:
        lines.append(f"- `{page['kind']}` `{page['id']}` — {page['url']} — [html](raw/{page['html']}) — [text](raw/{page['text']})")
    if not saved_pages:
        lines.append("- Страницы не были сохранены.")
    lines.append("")
    lines.append("## Скачанные документы")
    lines.append("")
    for doc in downloaded_docs:
        lines.append(f"- [{doc['file']}]({doc['file']}) — тип `{doc.get('kind', 'file')}` — источник: {describe_source(doc['source'])}")
    if not downloaded_docs:
        lines.append("- На собранных страницах компании и сделок не найдено прямых ссылок на документы для скачивания.")
    lines.append("")
    lines.append("## Что ещё сохранено")
    lines.append("")
    if (meta_dir / "company_details.json").exists():
        lines.append("- [metadata/company_details.json](metadata/company_details.json) — извлеченная JSON-модель карточки компании из CRM.")
    if (meta_dir / "deal_details.json").exists():
        lines.append("- [metadata/deal_details.json](metadata/deal_details.json) — извлеченные JSON-модели карточек сделок.")
    if (meta_dir / "entity_links.json").exists():
        lines.append("- [metadata/entity_links.json](metadata/entity_links.json) — полный реестр найденных CRM-сущностей и ссылок.")
    if (meta_dir / "income_contracts.json").exists():
        lines.append("- [metadata/income_contracts.json](metadata/income_contracts.json) — строки вкладки компании «Доходные договоры» с датами заключения и явными ссылками ДС на базовый договор; мастер выбирает действующую связку, покрывающую период проекта.")
    if (meta_dir / "tabs.json").exists():
        lines.append("- [metadata/tabs.json](metadata/tabs.json) — список вкладок и их внутренних loader URL.")
    if (meta_dir / "lazy_tabs.json").exists():
        lines.append("- [metadata/lazy_tabs.json](metadata/lazy_tabs.json) — отдельно загруженные ленивые вкладки карточек.")
    if (meta_dir / "timeline_highlights.json").exists():
        lines.append("- [metadata/timeline_highlights.json](metadata/timeline_highlights.json) — извлеченные события timeline с прямыми ссылками.")
    if (meta_dir / "related_entities.json").exists():
        lines.append("- [metadata/related_entities.json](metadata/related_entities.json) — список связанных карточек, которые удалось открыть автоматически.")
    if (meta_dir / "communications.tsv").exists():
        lines.append("- [metadata/communications.tsv](metadata/communications.tsv) — все найденные e-mail и телефоны по связанным контактам.")
    if (meta_dir / "documents.json").exists():
        lines.append("- [metadata/documents.json](metadata/documents.json) — реестр скачанных файлов и источников.")
    lines.append("- [metadata/run_report.json](metadata/run_report.json) — отчет выполнения: что открылось, что не открылось, ошибки и предупреждения.")
    if document_entrypoints:
        write_text_file(meta_dir / "document_entrypoints.json", json.dumps(document_entrypoints, ensure_ascii=False, indent=2))
        lines.append("- [metadata/document_entrypoints.json](metadata/document_entrypoints.json) — технические точки входа в генератор документов.")
    lines.append("")
    lines.append("## Статус")
    lines.append("")
    has_context = bool(saved_pages or deal_matches or entity_links or related_entities or downloaded_docs)
    if has_context:
        lines.append("- Основной файл содержит извлеченный CRM-контекст: карточку, сделки, связанные CRM-сущности, контакты, вкладки и документы в том объеме, который удалось получить прямыми HTTP-запросами.")
    else:
        lines.append("- CRM-контекст по этому варианту поиска не найден; файл фиксирует нулевой результат, а не доказанное отсутствие истории отношений.")
    warnings = run_report["warnings"]
    assert isinstance(warnings, list)
    if warnings:
        lines.append(f"- Есть предупреждения сборщика: `{len(warnings)}`; детали см. в [metadata/run_report.json](metadata/run_report.json).")
    lines.append("- Технические маршруты, сырые HTML-страницы и массовые контактные данные сохранены отдельно в архиве и metadata.")

    context_path = company_dir / "context.md"
    write_text_file(context_path, "\n".join(lines) + "\n")
    errors = run_report["errors"]
    assert isinstance(errors, list)
    serious_gap = not saved_pages or (bool(company_id) and not any(page["kind"] == "company" for page in saved_pages))
    save_run_report("partial" if errors or serious_gap else "ok")
    print(str(context_path))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("probe")

    contract_parser = subparsers.add_parser("contract")
    contract_parser.add_argument("--output")

    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("target")
    fetch_parser.add_argument(
        "--format",
        choices=("html", "text", "links"),
        default="text",
    )

    deals_parser = subparsers.add_parser("list-deals")
    deals_parser.add_argument("--client-contains")
    deals_parser.add_argument("--max-pages", type=int, default=50)

    deal_lookup_parser = subparsers.add_parser("find-deal-by-project-number")
    deal_lookup_parser.add_argument("--project-number", required=True)
    deal_lookup_parser.add_argument("--output", required=True)

    deal_context_parser = subparsers.add_parser("collect-deal-context")
    deal_context_input = deal_context_parser.add_mutually_exclusive_group(required=True)
    deal_context_input.add_argument("--project-number")
    deal_context_input.add_argument("--deal-id")
    deal_context_input.add_argument("--deal-url")
    deal_context_parser.add_argument("--output-dir", required=True)
    deal_context_parser.add_argument("--skip-document-downloads", action="store_true")

    entity_context_parser = subparsers.add_parser("collect-entity-context")
    entity_context_parser.add_argument("--entity-url", required=True)
    entity_context_parser.add_argument("--expected-kind", choices=("contact", "company", "deal", "dynamic", "lead", "quote", "smart_invoice"))
    entity_context_parser.add_argument("--output-dir", required=True)

    project_folder_parser = subparsers.add_parser("collect-project-folder")
    project_folder_parser.add_argument("--folder-url", required=True)
    project_folder_parser.add_argument("--output-dir", required=True)
    project_folder_parser.add_argument("--download-file-url", action="append", default=[])
    project_folder_parser.add_argument("--max-pages", type=int, default=200)

    chat_resolution_parser = subparsers.add_parser("record-deal-chat-resolution")
    chat_resolution_parser.add_argument("--deal-search-report", required=True)
    chat_resolution_parser.add_argument("--chat-url", required=True)
    chat_resolution_parser.add_argument("--chat-header", required=True)
    chat_resolution_parser.add_argument("--open-deal-url", required=True)
    chat_resolution_parser.add_argument("--resolved-tax-status", required=True, choices=("SMZ", "IP", "FL"))
    chat_resolution_parser.add_argument("--message-locator", action="append", required=True)
    chat_resolution_parser.add_argument("--message-text", required=True)
    chat_resolution_parser.add_argument("--reviewed-at", required=True)
    chat_resolution_parser.add_argument("--output", required=True)

    companies_parser = subparsers.add_parser("list-companies")
    companies_parser.add_argument("--name-contains")
    companies_parser.add_argument("--max-pages", type=int, default=50)

    income_contracts_parser = subparsers.add_parser("list-income-contracts")
    income_contracts_parser.add_argument("--company", required=True)
    income_contracts_parser.add_argument("--company-id")
    income_contracts_parser.add_argument("--company-card-url")
    income_contracts_parser.add_argument("--max-pages", type=int, default=50)
    income_contracts_parser.add_argument("--output", required=True)

    income_contract_download_parser = subparsers.add_parser("download-selected-income-contract")
    income_contract_download_parser.add_argument("--selection", required=True)
    income_contract_download_parser.add_argument("--output-dir", required=True)
    income_contract_download_parser.add_argument("--file-url")

    collect_parser = subparsers.add_parser("collect-company-context")
    collect_parser.add_argument("company_name", nargs="?", default="")
    collect_parser.add_argument("--company-id")
    collect_parser.add_argument("--output-dir", required=True)
    collect_parser.add_argument("--max-company-pages", type=int, default=50)
    collect_parser.add_argument("--max-deal-pages", type=int, default=50)
    collect_parser.add_argument("--max-related-cards", type=int, default=DEFAULT_MAX_RELATED_CARDS)
    collect_parser.add_argument("--mode", choices=COLLECT_MODES, default="full")
    collect_parser.add_argument("--skip-document-downloads", action="store_true")

    dossier_parser = subparsers.add_parser("build-company-dossier")
    dossier_parser.add_argument("company_name", nargs="?", default="")
    dossier_parser.add_argument("--company-id")
    dossier_parser.add_argument("--output-dir", required=True)
    dossier_parser.add_argument("--max-company-pages", type=int, default=50)
    dossier_parser.add_argument("--max-deal-pages", type=int, default=50)
    dossier_parser.add_argument("--max-related-cards", type=int, default=DEFAULT_MAX_RELATED_CARDS)
    dossier_parser.add_argument("--mode", choices=COLLECT_MODES, default="full")
    dossier_parser.add_argument("--skip-document-downloads", action="store_true")
    return parser


def main() -> int:
    load_env_file(ENV_PATH)
    args = build_parser().parse_args()
    if args.command == "contract":
        return command_contract(args.output)
    if args.command == "record-deal-chat-resolution":
        return command_record_deal_chat_resolution(
            args.deal_search_report,
            args.chat_url,
            args.chat_header,
            args.open_deal_url,
            args.resolved_tax_status,
            args.message_locator,
            args.message_text,
            args.reviewed_at,
            args.output,
        )
    client = BitrixSessionClient(
        env_required("B24_BASE_URL"),
        env_required("B24_LOGIN"),
        env_required("B24_PASSWORD"),
    )
    if args.command == "probe":
        return command_probe(client)
    if args.command == "fetch":
        return command_fetch(client, args.target, args.format)
    if args.command == "list-deals":
        return command_list_deals(client, args.client_contains, args.max_pages)
    if args.command == "find-deal-by-project-number":
        return command_find_deal_by_project_number(client, args.project_number, args.output)
    if args.command == "collect-deal-context":
        return command_collect_deal_context(
            client,
            args.output_dir,
            args.project_number,
            args.deal_id,
            args.deal_url,
            args.skip_document_downloads,
        )
    if args.command == "collect-entity-context":
        return command_collect_entity_context(
            client,
            args.output_dir,
            args.entity_url,
            args.expected_kind,
        )
    if args.command == "collect-project-folder":
        return command_collect_project_folder(
            client,
            args.output_dir,
            args.folder_url,
            args.download_file_url,
            args.max_pages,
        )
    if args.command == "list-companies":
        return command_list_companies(client, args.name_contains, args.max_pages)
    if args.command == "list-income-contracts":
        return command_list_income_contracts(
            client,
            args.company,
            args.max_pages,
            args.output,
            args.company_id,
            args.company_card_url,
        )
    if args.command == "download-selected-income-contract":
        return command_download_selected_income_contract(
            client,
            args.selection,
            args.output_dir,
            args.file_url,
        )
    if args.command in {"collect-company-context", "build-company-dossier"}:
        validate_dossier_output_dir(args.output_dir)
        try:
            return command_collect_company_context(
                client,
                args.company_name,
                args.company_id,
                args.output_dir,
                args.max_company_pages,
                args.max_deal_pages,
                args.mode,
                args.skip_document_downloads,
                args.max_related_cards,
            )
        except Exception as exc:
            write_failed_dossier_artifacts(
                args.output_dir,
                args.company_name,
                args.company_id,
                args.mode,
                exc,
            )
            raise
    raise SystemExit("Неизвестная команда")


if __name__ == "__main__":
    raise SystemExit(main())
