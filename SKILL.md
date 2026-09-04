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

`${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/.env`

## Главный сценарий: собрать всё по компании

Когда пользователь просит собрать контекст по компании из CRM, используй этот skill как основной инструмент.

Вход:

- название компании, например `'Название компании'`
- опционально точный `company_id`, если пользователь дал ссылку на карточку компании

Главная команда по названию компании:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" build-company-dossier \
  'Название компании' \
  --mode full \
  --skip-document-downloads
```

Главная команда по известной карточке компании:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" build-company-dossier \
  --company-id 698 \
  'Название компании' \
  --mode full \
  --skip-document-downloads
```

Режимы сбора:

- `quick` — быстрая проверка и базовый пакет: карточка компании, найденные сделки, основные снимки и документы без раскрытия ленивых вкладок и связанных карточек.
- `package` — ограниченный режим для мастер-сборки П4.0: точная карточка компании, её снимки и реестр доступных вкладок. Не обходит все сделки, timeline и связанные карточки компании; реестр доходных договоров и выбранный файл получают отдельными целевыми командами ниже.
- `full` — рабочий режим по умолчанию: карточка компании, сделки, ленивые вкладки сделок, связанные карточки из timeline, документы и metadata.
- `deep` — расширенный режим: дополнительно пытается раскрывать ленивые вкладки компании и связанных сущностей; используй осторожно, потому что он тяжелее для CRM.

`full` и `deep` ограничивают число открываемых связанных карточек параметром
`--max-related-cards` (по умолчанию 120), чтобы завершить dossier и зафиксировать
охват вместо бесконечного обхода CRM-связей. Если лимит достигнут, отчёт содержит
предупреждение; для пакета П4.0 это не заменяет целевой реестр доходных договоров.

Флаг `--skip-document-downloads` сохраняет страницы и metadata, но не скачивает
файлы. Для мастер-сборки П4.0 применять его на первом проходе обязательно: тип и
срок доходного договора определяются по общему списку, а при подтверждённом
отсутствии компании в нём — по вкладке карточки компании без чтения файла.
Позднее допускается точечно скачать только файл уже выбранного договора, если без
него нельзя получить одновременно полное и сокращённое юридические наименования.

Результат всегда сохраняется в явно указанную через `--output-dir` папку запуска:

- `bitrix24_company_contexts/<company-slug>/context.md`
- `bitrix24_company_contexts/<company-slug>/raw/`
- `bitrix24_company_contexts/<company-slug>/documents/`
- `bitrix24_company_contexts/<company-slug>/metadata/`

После каждого запуска обязательно проверяй:

- `metadata/run_report.json` — статус запуска, режим, ошибки, предупреждения, количество сохраненных страниц, вкладок и документов
- `context.md` — человекочитаемый основной контекст
- `metadata/lazy_tabs.json` — какие ленивые вкладки удалось раскрыть

Эти три файла и `metadata/documents.json` создаются всегда, в том числе при пустых
реестрах. Статус `running`, `failed` или `partial` в `run_report.json` нельзя
выдавать за готовый dossier: только `ok` подтверждает завершённый сбор. Перед новым
запуском последняя успешная версия этих основных файлов сохраняется в
`metadata/last-successful/`; raw-снимки не являются заменой итогового отчёта.

Что обязательно собирать:

- карточки компаний, найденные по названию или по `company_id`
- сделки из объединенного реестра: первичный поиск по гриду сделок, вкладка/связи карточки компании, timeline и redirect-ссылки на `/crm/deal/details/<id>/`
- вкладки карточки компании и их технические loader URL в `metadata/tabs.json`
- для определения типа доходного договора и срока его действия сначала обращаться к
  общему списку CRM
  `https://crm.prof-4.ru/page/dogovory/dokhodnye_dogovory/`; отфильтровать точную
  компанию, запросить сортировку по `Дата заключения` по возрастанию и сохранить
  строки, срок действия и подтверждение полноты в `metadata/income_contracts.json`;
  только если полное прохождение списка подтвердило нулевой результат по компании,
  открыть точную карточку компании и её вкладку `Доходные договоры`, взять тип из
  столбца `Форма договора` и сохранить fallback-provenance в том же metadata;
  timeline, Job Offer и локальный пример fallback не заменяют. Для мастер-сборки
  П4.0 не выбирать договор только по самой ранней дате: сохранить явные ссылки ДС
  на номер базового договора и сроки из CRM-строк. Мастер выбирает единственную
  действующую связку «базовый договор услуг/агентский + относящиеся к нему ДС о
  сроке», которая покрывает период проекта. Для типа и срока достаточно данных
  разрешённых CRM-строк; файл договора для этого не читать; чтение разрешено
  отдельно и только для юридических наименований
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

1. Для машинного потребителя сначала выполни `contract`: команда не требует логина и возвращает версию контракта, capabilities и признак read-only.
2. Run `probe` to verify login and common integration sections.
3. Check whether webhook/app setup is available.
4. If REST is still unavailable, choose the lightest viable portal path:
   - detail card fetch for one known entity
   - grid fragment fetch for bulk list reading
   - direct link extraction when exploring unknown sections
5. Generalize the path only after one concrete example works.

## Commands

Проверить машинный контракт без чтения CRM и без учетных данных:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" contract
```

Probe login and common integration entrypoints:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" probe
```

Fetch any authenticated page:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" fetch \
  "/crm/deal/details/${DEAL_ID}/" \
  --format text
```

Extract links from a page:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" fetch \
  /crm/company/list/ \
  --format links
```

Read deal rows through the lighter Bitrix grid channel:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" list-deals
```

Filter deals by client name substring:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" list-deals \
  --client-contains 'Название клиента' \
  --max-pages 25
```

Найти одну сделку для чата по номеру проекта из четырёх цифр:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" \
  find-deal-by-project-number --project-number '4623' \
  --output '<run>/crm-dossier/metadata/deal-project-search.json'
```

Команда выполняет только штатный поиск раздела «Сделки» (`FIND=4623`) и принимает
лишь единственную строку с заголовком `4623: …`. При нуле или нескольких строках
она сохраняет безопасный отчёт и завершается с блокером; глобальный поиск
Мессенджера использовать нельзя.

Собрать универсальный read-only контекст одной точной сделки по номеру проекта,
ID или ссылке:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" \
  collect-deal-context --project-number '4623' \
  --output-dir '<run>/crm-deal-context'
```

Вместо `--project-number` допустимы `--deal-id` и `--deal-url`. Команда сохраняет
`raw/`, `documents/` и машиночитаемые `metadata/deal.json`, `fields.json`, `files.json`,
`related_entities.json`, `tabs.json`, `run_report.json`. Она не содержит бизнес-правил потребителя.
Флаг `--skip-document-downloads` оставляет только реестр ссылок на файлы.
Внешний URL карточки сохраняется отдельно. Если во внешнем HTML нет ровно одной
машинной модели с ожидаемым ID сделки, команда автоматически повторяет запрос с
`IFRAME=Y&IFRAME_TYPE=SIDE_SLIDER`. Успех возможен только при ровно одной модели
ожидаемой сделки; модель другой сделки никогда не используется как fallback.
`fields.json` содержит и пустые зарегистрированные поля, а также доступные
`field_title`, `field_type`, `multiple`; полная извлеченная схема сохраняется в
`metadata/field_schema.json`.

Собрать ровно одну явно выбранную связанную CRM-сущность без поиска замены:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" \
  collect-entity-context --entity-url 'https://crm.example/crm/type/170/details/100/' \
  --output-dir '<run>/crm-role-context'
```

Команда применяет тот же outer/side-slider алгоритм и принимает только модель с
entity type и ID из переданного URL. Она предназначена для роли, исполнителя,
контакта, компании или иной точной связанной карточки; перед чтением выполняется
авторизация. `fields.json` и `metadata/field_schema.json` включают все
зарегистрированные поля, которые раскрыла карточка, в том числе пустые; для
стандартных полей сохраняются стабильные code/title/type metadata. Бизнес-правила
остаются у потребителя.

Собрать полный инвентарь одной точной папки проекта Bitrix Disk:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" \
  collect-project-folder \
  --folder-url 'https://crm.example/docs/shared/path/project/' \
  --output-dir '<run>/project-folder-context'
```

Команда проходит видимые страницы и вложенные папки, записывает
`project-folder-files.json`, raw HTML страниц и `run_report.json`. Сбор инвентаря
не зависит от лимита скачиваний. Для скачивания отдельного уже выбранного файла
передай его URL повторяемым `--download-file-url`; мост проверит, что URL есть в
инвентаре точной папки, сохранит исходное имя, размер, MIME и SHA-256 и не примет
HTML авторизации/ошибки за файл. Команда не выбирает «обоснование» или иной
бизнес-документ самостоятельно.

## Динамический чат сделки для налогового статуса

Если нужен статус из чата, HTTP-снимок страницы чата не является доказательством:
сообщения и ссылка `ОТКРЫТЬ СДЕЛКУ` загружаются динамически. После успешного
`find-deal-by-project-number` открыть только `selected_deal.deal_url` в
авторизованной интерактивной браузерной сессии, нажать значок мессенджера в шапке
карточки и прочитать видимый чат.

Убедиться одновременно в трёх фактах:

- заголовок чата точно равен `Сделка: <название найденной сделки>`;
- ссылка `ОТКРЫТЬ СДЕЛКУ` ведёт на тот же ID карточки (параметры IFRAME допустимы);
- в сообщении есть явное финальное назначение `СМЗ`, `ИП` или `ФЛ`, а не вопрос,
  обсуждение или незавершённое уточнение.

Затем сохранить доказательство для мастер-сборщика. Полный текст сообщения в
отчёт не попадает: сохраняются его SHA-256 и локатор.

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" \
  record-deal-chat-resolution \
  --deal-search-report '<run>/crm-dossier/metadata/deal-project-search.json' \
  --chat-url '<видимый URL открытого чата>' \
  --chat-header 'Сделка: 4623: <точное название>' \
  --open-deal-url 'https://crm.prof-4.ru/crm/deal/details/<ID>/' \
  --resolved-tax-status SMZ \
  --message-locator 'chat:<ID>/message:<ID или иной видимый локатор>' \
  --message-text '<текст явного финального назначения>' \
  --reviewed-at 'YYYY-MM-DD' \
  --output '<run>/crm-dossier/metadata/tax-status-chat-resolution.json'
```

Команда не делает сетевых запросов и не пытается угадать статус. Если заголовок,
обратная ссылка, дата или явное назначение не подтверждены, она сохраняет
`UNRESOLVED`; мастер обязан остановить маршрут. При доступном браузерном управлении
в Codex этот этап выполняет агент, а не человек вручную. Если интерактивной
авторизованной сессии нет, сохранить точный блокер
`CRM_DEAL_CHAT_INTERACTIVE_SESSION_UNAVAILABLE`; не подменять чат письмом, брифом
или HTTP-снимком.

List company cards by name substring:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" list-companies \
  --name-contains 'Название клиента'
```

Собрать реестр доходных договоров точной компании по обязательному маршруту CRM:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" \
  list-income-contracts \
  --company 'Точное название компании' \
  --output '<run>/metadata/income_contracts.json'
```

Продолжать только если команда подтверждает строки компании, сортировку и полное
прохождение страниц. Если компания не найдена после полного просмотра общего
списка, команда сама находит точную карточку компании и читает вкладку `Доходные
договоры`; при известном ID передать `--company-id <ID>`. При наличии в
сохранённом dossier точного URL карточки также передать
`--company-card-url <URL>`. Сохранённый URL имеет приоритет. Если обычный URL
возвратил оболочку портала без карточки или вкладки, сборщик автоматически
повторяет запрос с `IFRAME=Y&IFRAME_TYPE=SIDE_SLIDER` и сохраняет
фактически сработавший URL. Тип брать только из
столбца `Форма договора`. Если срок действия отсутствует в видимых полях
разрешённого источника, остановиться. Для ДС сохранить номер базового договора, на
который оно прямо ссылается; не считать ДС продлением только по более поздней дате
или похожему названию. Не классифицировать текст файла и не получать тип/срок из
Job Offer или расходного договора.

Если после выбора договора нужны его полное и сокращённое юридические наименования,
скачать только файл выбранной строки:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" \
  download-selected-income-contract \
  --selection '<run>/metadata/income-contract-selection.json' \
  --output-dir '<run>/private-income-contract'
```

При нескольких файлах команда останавливается; после просмотра списка повторить с
точным `--file-url`. Не запускать общий сбор документов и не скачивать ДС/другие
договоры ради наименований.

Если выбранный файл — PDF, сначала извлечь его текстовый слой мастерским
`extract_income_contract_legal_names.py`. Если слой пуст, открыть и визуально просмотреть
именно этот PDF через `$pdf`, зафиксировать SHA-256, номера страниц и точные локаторы.
Из файла разрешено вынести только полное и сокращённое юридические наименования; иные
реквизиты не извлекать и не переносить. Визуальный отчёт без совпадающего SHA запрещён.

Collect a reusable CRM context package for one company:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" collect-company-context \
  'Название клиента' \
  --output-dir '<run>/crm-dossier'
```

Collect a context package by exact company id when you already know the card:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/bitrix24-session-bridge/scripts/bitrix24_session_client.py" collect-company-context \
  --company-id 12345 \
  'Название клиента' \
  --output-dir '<run>/crm-dossier'
```

The selected output directory creates:

- `bitrix24_company_contexts/<company-slug>/context.md`
- `bitrix24_company_contexts/<company-slug>/raw/`
- `bitrix24_company_contexts/<company-slug>/documents/`
- `bitrix24_company_contexts/<company-slug>/metadata/`

## CRM Task Paths

Use these routes as the first place to look when solving CRM tasks:

- Income contract list:
  - `/page/dogovory/dokhodnye_dogovory/`

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
5. Открыть общий список
   `https://crm.prof-4.ru/page/dogovory/dokhodnye_dogovory/`, отфильтровать точную
   компанию, отсортировать `Дата заключения` по возрастанию и сохранить
   `metadata/income_contracts.json`. Если полное прохождение списка подтвердило,
   что компании нет, открыть точную карточку компании и вкладку `Доходные договоры`;
   форму взять из одноимённого столбца. Для базового договора и каждого связанного
   ДС зафиксировать срок и явную ссылку ДС на номер базового договора. Если источник,
   полнота, сортировка, соответствие компании, срок или связь ДС не подтверждены,
   договорную связку не выбирать.
6. For deal cards, fetch accessible lazy tabs through their `serviceUrl` and save them as separate snapshots.
7. Extract file/document links from those pages.
8. Download documents into the company folder only when they are independently
   needed for the dossier. For the P4.0 contract-package route, use
   `--skip-document-downloads`: общий список либо разрешённая fallback-вкладка
   достаточны для типа и срока. Если для пакета П4.0 нужны оба юридических
   наименования, использовать только `download-selected-income-contract` после
   выбора базового договора. When document collection is required for another task:
   - CRM user-field files through `crm.controller.item.getFile`
   - direct file links
   - Bitrix Disk project folders discovered in CRM fields
   CRM user-field files must be downloaded before bulk Bitrix Disk folders and must
   not be skipped merely because the generic document download limit was reached.
9. Save machine-readable metadata:
   - tabs and internal loader URLs
   - fetched lazy tabs
   - timeline highlights with direct links
   - communications of related contacts
   - downloaded documents registry
10. Build one `context.md` file that links:
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
- Основной реестр доходных договоров для определения типа и срока действия:
  - `/page/dogovory/dokhodnye_dogovory/`
- Разрешённый fallback только при полном нулевом поиске компании в основном реестре:
  - `/crm/company/details/<id>/` → вкладка `Доходные договоры` → столбец `Форма договора`

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
