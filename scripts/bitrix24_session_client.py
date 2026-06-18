#!/usr/bin/env python3
"""Сессионный клиент для Bitrix24.

Выполняет вход через стандартную веб-форму Bitrix24, хранит cookies
и собирает CRM-контекст по компании, включая карточки, историю, документы
и архивные снимки страниц.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import http.client
import http.cookiejar
import json
import os
import pathlib
import re
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
DEFAULT_OUTPUT_DIR = pathlib.Path.cwd() / "bitrix24_company_contexts"
COLLECT_MODES = ("quick", "full", "deep")
DEFAULT_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_DOCUMENT_DOWNLOADS = 120
GENERATED_METADATA_FILES = (
    "communications.tsv",
    "company_details.json",
    "deal_details.json",
    "deal_matches.json",
    "document_entrypoints.json",
    "documents.json",
    "entity_links.json",
    "lazy_tabs.json",
    "related_entities.json",
    "run_report.json",
    "tabs.json",
    "timeline_highlights.json",
)
CRM_ENTITY_TYPES = {
    "1": "lead",
    "2": "deal",
    "3": "contact",
    "4": "company",
    "7": "quote",
    "31": "smart_invoice",
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
        return url_or_path
    if not url_or_path.startswith("/"):
        url_or_path = "/" + url_or_path
    return base_url + url_or_path


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
        r'<tr class="main-grid-row main-grid-row-body".*?data-id="(?P<id>\d+)".*?</tr>',
        re.S,
    )
    for match in row_re.finditer(raw_html):
        row_html = match.group(0)
        deal_id = match.group("id")
        detail_match = re.search(r'href="/crm/deal/details/\d+/">(?P<title>.*?)</a>', row_html, re.S)
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
                "url": f"/crm/deal/details/{deal_id}/",
            }
        )
    return rows


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

    redirect_re = re.compile(
        r'"text":"(?P<title>(?:\\.|[^"])*)".*?"action":\{"type":"redirect","value":"(?P<url>(?:\\.|[^"])*)"\}',
        re.S,
    )
    for match in redirect_re.finditer(raw_html):
        path = normalize_crm_path(match.group("url"))
        title = js_unescape(match.group("title"))
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
        return ".docx"
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
        "/crm/deal/details/14325/",
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


def command_collect_company_context(
    client: BitrixSessionClient,
    company_name: str,
    company_id: str | None,
    output_dir: str | None,
    max_company_pages: int,
    max_deal_pages: int,
    mode: str,
) -> int:
    root_dir = ensure_dir(pathlib.Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR)
    company_key = company_name if company_name else f"company-{company_id}"
    company_dir = ensure_dir(root_dir / slugify(company_key))
    raw_dir = ensure_dir(company_dir / "raw")
    docs_dir = ensure_dir(company_dir / "documents")
    meta_dir = ensure_dir(company_dir / "metadata")
    disk_dir = ensure_dir(docs_dir / "bitrix_disk")
    crm_files_dir = ensure_dir(docs_dir / "crm_fields")
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
    document_entrypoints: dict[str, str] = {}
    related_detail_links: list[str] = []
    document_download_limit = env_int("B24_MAX_DOCUMENT_DOWNLOADS", DEFAULT_MAX_DOCUMENT_DOWNLOADS)
    document_download_limit_warned = False

    run_report: dict[str, object] = {
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
    if company_name:
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

    if mode == "quick":
        related_detail_links = []

    for index, link in enumerate(related_detail_links, start=1):
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
    if communications:
        save_tsv(meta_dir / "communications.tsv", communications, ["contact_id", "address_id", "type", "value_type", "value"])
    if timeline_highlights:
        write_text_file(meta_dir / "timeline_highlights.json", json.dumps(timeline_highlights, ensure_ascii=False, indent=2))
    if tab_inventory:
        write_text_file(meta_dir / "tabs.json", json.dumps(tab_inventory, ensure_ascii=False, indent=2))
    if related_entities:
        write_text_file(meta_dir / "related_entities.json", json.dumps(related_entities, ensure_ascii=False, indent=2))
    if lazy_tab_pages:
        write_text_file(meta_dir / "lazy_tabs.json", json.dumps(lazy_tab_pages, ensure_ascii=False, indent=2))
    if downloaded_docs:
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

    companies_parser = subparsers.add_parser("list-companies")
    companies_parser.add_argument("--name-contains")
    companies_parser.add_argument("--max-pages", type=int, default=50)

    collect_parser = subparsers.add_parser("collect-company-context")
    collect_parser.add_argument("company_name", nargs="?", default="")
    collect_parser.add_argument("--company-id")
    collect_parser.add_argument("--output-dir")
    collect_parser.add_argument("--max-company-pages", type=int, default=50)
    collect_parser.add_argument("--max-deal-pages", type=int, default=50)
    collect_parser.add_argument("--mode", choices=COLLECT_MODES, default="full")

    dossier_parser = subparsers.add_parser("build-company-dossier")
    dossier_parser.add_argument("company_name", nargs="?", default="")
    dossier_parser.add_argument("--company-id")
    dossier_parser.add_argument("--output-dir")
    dossier_parser.add_argument("--max-company-pages", type=int, default=50)
    dossier_parser.add_argument("--max-deal-pages", type=int, default=50)
    dossier_parser.add_argument("--mode", choices=COLLECT_MODES, default="full")
    return parser


def main() -> int:
    load_env_file(ENV_PATH)
    args = build_parser().parse_args()
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
    if args.command == "list-companies":
        return command_list_companies(client, args.name_contains, args.max_pages)
    if args.command in {"collect-company-context", "build-company-dossier"}:
        return command_collect_company_context(
            client,
            args.company_name,
            args.company_id,
            args.output_dir,
            args.max_company_pages,
            args.max_deal_pages,
            args.mode,
        )
    raise SystemExit("Неизвестная команда")


if __name__ == "__main__":
    raise SystemExit(main())
