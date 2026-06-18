---
name: bitrix24-session-bridge
description: "Log into a self-hosted or on-prem Bitrix24 portal with a user login and password, keep a browser-like session, and collect a full CRM company dossier: company cards, deals, timeline, related smart-process entities, contracts, attachments, Bitrix Disk folders, raw snapshots, metadata, and a readable Russian context.md. Use when the user asks to gather everything available in Bitrix24 CRM about a company, especially when webhook/OAuth access is unavailable or not yet configured."
---

# Bitrix24 Session Bridge

Use this skill when normal REST access is missing and the user only has a working Bitrix24 web login.

## Язык

- Всегда работай только на русском языке.
- Все пояснения, промежуточные выводы, итоговые файлы, заголовки, подписи, описания, заметки и сопроводительные тексты формируй только на русском языке.
- Английский допускается только там, где это часть технического идентификатора, URL, имени поля, пути, команды, переменной окружения или исходного интерфейса Bitrix24.
- Если исходные данные частично на английском, интерпретацию и итоговое изложение всё равно давай на русском языке.

## Positioning

- Prefer inbound webhook or proper OAuth whenever available.
- Use this skill as a bridge:
  - to verify whether webhook/app setup is available for the current user
  - to inspect CRM pages and portal sections after login
  - to extract CRM data from lists and detail cards
  - to discover lighter internal request paths used by Bitrix grids
- Session scraping is more fragile than REST. Treat DOM selectors, grid IDs, `bxajaxid`, and internal URLs as implementation details that may change.

## Runtime

Required environment:

- `B24_BASE_URL`
- `B24_LOGIN`
- `B24_PASSWORD`

The script auto-loads these variables from:

`~/.codex/skills/bitrix24-session-bridge/.env`

## Главный сценарий: собрать всё по компании

Когда пользователь просит собрать контекст по компании из CRM, используй этот skill как основной инструмент.

Вход:

- название компании, например `'Название компании'`
- опционально точный `company_id`, если пользователь дал ссылку на карточку компании

Главная команда по названию компании:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py build-company-dossier \
  'Название компании' \
  --mode full
```

Главная команда по известной карточке компании:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py build-company-dossier \
  --company-id 698 \
  'Название компании' \
  --mode full
```

Режимы сбора:

- `quick` — быстрая проверка и базовый пакет: карточка компании, найденные сделки, основные снимки и документы без раскрытия ленивых вкладок и связанных карточек.
- `full` — рабочий режим по умолчанию: карточка компании, сделки, ленивые вкладки сделок, связанные карточки из timeline, документы и metadata.
- `deep` — расширенный режим: дополнительно пытается раскрывать ленивые вкладки компании и связанных сущностей; используй осторожно, потому что он тяжелее для CRM.

Результат всегда сохраняется в папку:

- `bitrix24_company_contexts/<company-slug>/context.md`
- `bitrix24_company_contexts/<company-slug>/raw/`
- `bitrix24_company_contexts/<company-slug>/documents/`
- `bitrix24_company_contexts/<company-slug>/metadata/`

После каждого запуска обязательно проверяй:

- `metadata/run_report.json` — статус запуска, режим, ошибки, предупреждения, количество сохраненных страниц, вкладок и документов
- `context.md` — человекочитаемый основной контекст
- `metadata/lazy_tabs.json` — какие ленивые вкладки удалось раскрыть

Что обязательно собирать:

- карточки компаний, найденные по названию или по `company_id`
- сделки из объединенного реестра: первичный поиск по гриду сделок, вкладка/связи карточки компании, timeline и redirect-ссылки на `/crm/deal/details/<id>/`
- вкладки карточки компании и их технические loader URL в `metadata/tabs.json`
- ленивые вкладки карточек сделок, которые открываются через `serviceUrl`, сохраняй отдельными HTML/TXT снимками в `raw/` и реестром в `metadata/lazy_tabs.json`
- связанные сущности из timeline: сделки, доходные договоры, расходные договоры, ДС, заявки и другие смарт-процессы
- архивные HTML/TXT снимки карточек в `raw/`
- коммуникации контактов в `metadata/communications.tsv`
- документы из CRM user-field файлов через `crm.controller.item.getFile`
- документы из Bitrix Disk папок, найденных в CRM-полях
- реестр документов в `metadata/documents.json`
- отчет выполнения в `metadata/run_report.json`

Правило итогового файла:

- `context.md` должен быть читабельным основным контекстом для работы с компанией
- технические маршруты, loader URL, сырые HTML и массовые контакты держи в `raw/` и `metadata/`
- в `context.md` обязательно оставляй ссылки на все скачанные документы

## Core Workflow

1. Run `probe` to verify login and common integration sections.
2. Check whether webhook/app setup is available.
3. If REST is still unavailable, choose the lightest viable portal path:
   - detail card fetch for one known entity
   - grid fragment fetch for bulk list reading
   - direct link extraction when exploring unknown sections
4. Generalize the path only after one concrete example works.

## Commands

Probe login and common integration entrypoints:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py probe
```

Fetch any authenticated page:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py fetch \
  /crm/deal/details/14325/ \
  --format text
```

Extract links from a page:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py fetch \
  /crm/company/list/ \
  --format links
```

Read deal rows through the lighter Bitrix grid channel:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py list-deals
```

Filter deals by client name substring:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py list-deals \
  --client-contains 'Название клиента' \
  --max-pages 25
```

List company cards by name substring:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py list-companies \
  --name-contains 'Название клиента'
```

Collect a reusable CRM context package for one company:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py collect-company-context \
  'Название клиента'
```

Collect a context package by exact company id when you already know the card:

```bash
python3 ~/.codex/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py collect-company-context \
  --company-id 12345 \
  'Название клиента'
```

By default this creates:

- `bitrix24_company_contexts/<company-slug>/context.md`
- `bitrix24_company_contexts/<company-slug>/raw/`
- `bitrix24_company_contexts/<company-slug>/documents/`
- `bitrix24_company_contexts/<company-slug>/metadata/`

## CRM Task Paths

Use these routes as the first place to look when solving CRM tasks:

- Deal list:
  - `/crm/deal/list/`
- Deal detail:
  - `/crm/deal/details/<id>/`
- Company list:
  - `/crm/company/list/`
- Company detail:
  - `/crm/company/details/<id>/`
- Contact detail:
  - `/crm/contact/details/<id>/`

For each list page, inspect:

- visible pagination links
- `bxajaxid` next-page links
- grid ID and grid settings
- row HTML structure
- embedded `serviceUrl`

## Preferred Paths By Task Type

### 1. Check access and setup

Use:

- `/market/`
- `/market/hooks/`
- `/market/category/local/`

Look for:

- whether the page opens after login
- whether it redirects back to auth
- whether admin-only endpoints return `403`

### 2. Read one known CRM entity

Use a direct card URL first:

- `/crm/deal/details/<id>/`
- `/crm/company/details/<id>/`
- `/crm/contact/details/<id>/`

Start with `fetch --format text`, then switch to `html` if you need structure or embedded config.

### 3. Read many deals

Prefer the grid fragment path rather than full page reloads.

Current discovered mechanism:

- initial page: `/crm/deal/list/`
- extract `bxajaxid`
- then request:
  - `/crm/deal/list/?by=&order=&page=N&bxajaxid=<value>`

This returns the lighter `main-grid` fragment and is the preferred bulk-read path for deals.

### 4. Find entities by name

Use a list page and lightweight pagination first:

- companies: `/crm/company/list/?page=N`
- deals: `/crm/deal/list/?page=N&bxajaxid=<value>`

Only build a special-purpose extractor after the entity name search has worked at least once.

### 5. Build a company context package

When the goal is "give me everything we know in CRM about company X", follow this order:

1. Find matching company cards by name.
2. Find matching deals by company name in the deal grid.
3. Add deal links from the company timeline and related-card redirects to the same deal registry, deduplicated by deal id.
4. Save raw HTML and text for:
   - company cards
   - deal cards
   - related cards found through timeline redirects
5. For deal cards, fetch accessible lazy tabs through their `serviceUrl` and save them as separate snapshots.
6. Extract file/document links from those pages.
7. Download documents into the company folder:
   - CRM user-field files through `crm.controller.item.getFile`
   - direct file links
   - Bitrix Disk project folders discovered in CRM fields
   CRM user-field files are priority evidence, especially income contracts. They must be downloaded before bulk Bitrix Disk folders and must not be skipped merely because the generic document download limit was reached.
8. Save machine-readable metadata:
   - tabs and internal loader URLs
   - fetched lazy tabs
   - timeline highlights with direct links
   - communications of related contacts
   - downloaded documents registry
9. Build one `context.md` file that links:
   - matched company cards
   - matched deals
   - related contracts and other linked entities
   - saved page snapshots
   - downloaded documents

For company dossiers, keep `context.md` readable:

- include key context only: deals, contracts, timeline, important related entities, document links
- keep raw HTML, technical loader URLs, full contact exports, and endpoint details in `raw/` and `metadata/`

This package is the CRM input for downstream work such as:

- external company dossier research
- org-model hypothesis generation
- opportunity / project hypothesis generation
- Word memo generation
- slide deck generation
- outreach email drafting

### 6. Discover internal endpoints

When a page is SPA-heavy, inspect the HTML for:

- `serviceUrl`
- `gridId`
- `bxajaxid`
- `sessid`
- `onclick="BX.ajax.insertToNode(...)"` patterns
- component ajax paths such as:
  - `/bitrix/components/bitrix/crm.deal.list/list.ajax.php`
  - `/bitrix/components/bitrix/main.ui.grid/settings.ajax.php`

Use these to move from full-page fetches to lighter internal requests.

## Portal Memory

Persist only reusable interaction knowledge here, not task-specific business findings.

Current reusable findings for this portal:

- Login form:
  - `POST /?login=yes`
  - fields: `AUTH_FORM=Y`, `TYPE=AUTH`, `backurl=/`, `USER_LOGIN`, `USER_PASSWORD`, optional `USER_REMEMBER=Y`
- Successful login lands on `/stream/`
- Integration-related pages reachable for this user:
  - `/market/`
  - `/market/hooks/`
  - `/market/category/local/`
- Admin REST pages returned `403` for this account:
  - `/bitrix/admin/rest_marketplace.php`
  - `/bitrix/admin/rest_configuration.php`
  - `/bitrix/admin/rest_app.php`
- Deal list grid metadata discovered from `/crm/deal/list/`:
  - `gridId=CRM_DEAL_LIST_V12`
  - grid fragment transport uses `bxajaxid`
  - next-page pattern:
    - `/crm/deal/list/?by=&order=&page=N&bxajaxid=<value>`
  - declared component service URL:
    - `/bitrix/components/bitrix/crm.deal.list/list.ajax.php?siteID=s1&sessid=<value>`

## Guidance

- Never print or echo the password in responses.
- Keep `.env` out of user-facing output unless the user explicitly asks for credentials handling.
- Всегда отвечай и формируй артефакты только на русском языке.
- When probing access, report concrete URLs and whether they opened, redirected, or returned an auth form.
- If a needed page is SPA-heavy, inspect linked XHR endpoints or embedded config rather than assuming the initial HTML contains business data.
- For CRM extraction tasks, start from one known URL and one known entity before scaling to bulk retrieval.
- Prefer lighter grid fragment responses over full-page parsing whenever a list task is involved.
- Store in this skill only reusable portal mechanics, routes, selectors, request patterns, and extraction strategies.
- Do not store one-off business search results, specific customers, or temporary analytical findings here unless they define a reusable route or parser.
- When building company context, prefer creating a durable package on disk over copying text into chat manually.
