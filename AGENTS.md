# AGENTS.md — Telegram bot для подготовки еженедельного собрания

## Роль агента

Ты работаешь как senior Python engineer/product engineer. Нужно реализовать Telegram-бота, который помогает подготовить **одно еженедельное собрание** по YAML-схеме. В проекте нет множества собраний, организаций или контекстов: один запущенный бот = одно регулярное собрание и одна актуальная карточка текущей недели.

Бот должен быть надежным, простым в сопровождении, schema-driven и пригодным для волонтерского использования: минимум ручной работы, максимум контролируемых подтверждений перед изменением данных.

## Главная цель

Сделать Python-приложение на `python-telegram-bot`, которое:

1. При запуске принимает два файла:
   - `--app-config <path>` — конфиг приложения.
   - `--meeting-schema <path>` — YAML-схема карточки собрания.
2. Хранит текущую карточку собрания и архив карточек прошлых недель.
3. Позволяет смотреть статус подготовки, в том числе PDF-отчетом.
4. Позволяет editor/admin заполнять карточку командами, текстом в свободной форме и голосовыми сообщениями.
5. Перед записью изменений всегда показывает preview и требует подтверждения кнопкой.
6. Не отправляет сообщения в LLM для неодобренных или заблокированных пользователей/чатов.
7. В групповых чатах работает строго read-only.
8. Отправляет editor/admin мягкие напоминания за заданное число часов до дедлайна.

## Технологический стек

Обязательно:

- Python 3.12+.
- `python-telegram-bot` 21+ или актуальная стабильная версия.
- SQLite.
- OpenAI-compatible клиент через AITUNNEL.
- Dockerfile и `docker-compose.yml`.
- `pytest` для тестов.
- `ruff` для lint/format.
- `mypy` желательно, хотя бы для core-модулей.

Рекомендуемые библиотеки:

- `pydantic` / `pydantic-settings` для конфигов и валидации.
- `PyYAML` или `ruamel.yaml` для схемы.
- `SQLAlchemy` + Alembic или простой слой поверх `sqlite3`/`aiosqlite`. Предпочтительно SQLAlchemy Core/ORM, если не усложняет проект.
- `openai` SDK с `AsyncOpenAI`.
- `APScheduler` или `JobQueue` из `python-telegram-bot` для напоминаний.
- `reportlab` для PDF.
- `python-dotenv` только для локальной разработки.

## Запуск

Поддержать запуск:

```bash
python -m meeting_bot \
  --app-config ./config/app.yaml \
  --meeting-schema ./config/service_schema.yaml
```

Docker Compose:

```bash
docker compose up --build
```

Контейнер должен монтировать:

- конфиг приложения;
- YAML-схему собрания;
- volume с SQLite базой;
- при необходимости volume для временных PDF/аудио-файлов.

## Конфиг приложения

Сделать пример `config/app.example.yaml`.

Минимальный формат:

```yaml
telegram:
  token: "123:telegram-token"
  admin_user_id: 123456789

app:
  timezone: "Europe/Moscow"
  database_path: "./data/meeting_bot.sqlite3"
  log_level: "INFO"
  default_parse_mode: "HTML"

meeting:
  week_starts_on: 1        # 1 = Monday, 7 = Sunday
  service_day: 7           # день собрания внутри недели
  archive_after_service: true

notifications:
  enabled: true
  remind_before_hours: 3
  scheduler_interval_seconds: 60

llm:
  enabled: true
  base_url: "https://api.aitunnel.ru/v1/"
  api_key: "sk-aitunnel-xxx"
  text_model: "claude-sonnet-4-5"
  audio_model: "whisper-large-v3-turbo"
  request_timeout_seconds: 60
  max_retries: 3

pdf:
  font_path: "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
  output_dir: "./data/reports"
```

Правила:

- Не хардкодить секреты.
- Не логировать токены.
- `telegram.admin_user_id` — root-admin. Только он автоматически получает роль `admin` при первом запуске.
- Через интерфейс можно выдавать только `viewer` и `editor`. Роль `admin` через Telegram-команды не выдается.

## Схема собрания

YAML-схема имеет верхний формат:

```yaml
version: "4.0"
title: "Список пунктов для проверки к собранию 4.0"
blocks:
  - id: main_speaker
    title: Главный спикер
    type: required
    fields:
      name:
        label: Имя
        allowed_values: [...]
        ready_if: [...]
        deadline:
          day: 3
          hour: 10
          minute: 0
```

Нельзя завязывать код на конкретные блоки из текущей схемы. Нужно поддержать произвольные блоки, если они соответствуют этому контракту:

- `blocks[]` — список блоков.
- `block.id` — стабильный технический id.
- `block.title` — человекочитаемое название.
- `block.type` — `required` для обязательного одиночного блока, `optional` для опционального одиночного блока, `multiple` для повторяемого блока.
- `block.fields` — словарь полей.
- `field.label` — человекочитаемое название поля.
- `field.allowed_values` — подсказки допустимых значений.
- `field.ready_if` — правила готовности.
- `field.deadline` — `null` или объект `{day, hour, minute}`.

При изменении YAML-схемы приложение не должно падать. Если появились новые блоки/поля, они должны появиться в карточке и PDF. Если поле удалено из схемы, старое значение можно хранить в JSON, но не показывать в актуальном статусе, если поля больше нет в схеме.

## Модель данных карточки

Использовать SQLite и хранить карточку schema-driven. Не делать отдельную колонку под каждое поле схемы.

Рекомендуемая модель хранения:

### `meeting_cards`

- `id` integer primary key
- `week_start_date` text, ISO date
- `schema_version` text
- `schema_hash` text
- `data_json` text, JSON
- `created_at` text
- `updated_at` text
- `archived_at` text nullable

`data_json`:

```json
{
  "blocks": {
    "main_speaker": {
      "fields": {
        "name": "Иван Иванов",
        "microphone": "Хедсет"
      }
    },
    "additional_blocks": [
      {
        "entry_id": "uuid",
        "title": "Интервью с гостем",
        "fields": {
          "block_type": "Ток-шоу",
          "participants": "..."
        }
      }
    ],
    "announcements": []
  }
}
```

Правила:

- Для `type: required` хранить объект `{fields: {...}}`.
- Для `type: optional` хранить `null`/отсутствие, пока блок не заполнен; при заполнении хранить объект `{fields: {...}}`.
- Для `type: multiple` хранить список экземпляров.
- Пустой `multiple`-блок и незаполненный `optional`-блок не считаются незаполненными и не должны попадать в PDF/status.
- Если пользователь добавил экземпляр `multiple`-блока, все поля этого экземпляра оцениваются по обычным правилам готовности.
- Старые карточки прошлых недель не перезаписывать при старте новой недели.

### `users`

- `telegram_user_id` integer unique
- `username` text nullable
- `full_name` text nullable
- `role` text: `viewer`, `editor`, `admin`
- `status` text: `pending`, `approved`, `rejected`, `blocked`
- `created_at`, `updated_at`
- `approved_by` nullable
- `approved_at` nullable

### `chats`

- `chat_id` integer unique
- `chat_type` text: `private`, `group`, `supergroup`, `channel`
- `title` text nullable
- `status` text: `pending`, `approved`, `blocked`
- `read_only` boolean
- `created_at`, `updated_at`

Для групп `read_only=true` всегда.

### `pending_changes`

- `id` integer primary key
- `created_by_user_id` integer
- `chat_id` integer
- `week_start_date` text
- `patch_json` text
- `preview_text` text
- `status` text: `pending`, `approved`, `cancelled`, `expired`
- `telegram_message_id` integer nullable
- `created_at`, `resolved_at`

### `audit_log`

Фиксировать все изменения карточки, изменение ролей, блокировки, одобрения пользователей и чатов.

### `notification_state`

- `card_id`
- `block_id`
- `entry_id` nullable
- `field_id`
- `deadline_at`
- `reminder_kind`
- `sent_to_user_id`
- `sent_at`

Это нужно, чтобы не спамить одинаковыми уведомлениями.

## Неделя и архив

- Текущая карточка определяется по `week_start_date` в timezone из конфига.
- Если карточки текущей недели нет — создать пустую карточку на основе текущей схемы.
- Карточки прошлых недель должны оставаться в базе.
- Должна быть команда просмотра архива.
- Если схема изменилась, архивные карточки показывать с тем `schema_version/schema_hash`, с которым они были созданы, если сохраненная схема доступна. Если схема не сохранена, показывать best-effort по текущей схеме и честно писать, что схема отличается.

Рекомендуется добавить таблицу `schemas`:

- `schema_hash`
- `schema_version`
- `schema_json`
- `created_at`

## Правила готовности

Реализовать модуль `readiness.py`, покрытый тестами.

Поле считается готовым, если его значение удовлетворяет `ready_if`.

Поддержать минимум такие правила:

- `Не пусто` — значение существует и после trim не пустое.
- `Любое значение` — любое заданное значение, включая произвольный текст.
- `Любое значение или его отсутствие` — поле опционально, всегда ready/neutral; не подсвечивать как проблему.
- Конкретные значения: `Да`, `Нет`, `Не требуется`, `В процессе`, etc. — ready только если значение равно одному из ready values.
- Placeholder вида `<Название>`, `<N>`, `<Другое>` — любое непустое значение, кроме явных промежуточных статусов типа `В процессе`, если `В процессе` не указан в `ready_if`.
- Если `deadline: null`, поле не должно становиться overdue.

Статусы поля:

- `ready` — готово.
- `optional` — опционально/не требуется оценка.
- `missing` — не заполнено.
- `in_progress` — значение есть, но оно не удовлетворяет `ready_if`, например `В процессе`.
- `due_today` — не готово, дедлайн сегодня.
- `overdue` — не готово, дедлайн уже прошел.
- `not_due` — не готово, дедлайн в будущем.

Важно: дедлайны мягкие. Они не блокируют заполнение и не запрещают изменения. Они влияют только на приоритет уведомлений и подсветку.

## Подсветка PDF/status

Команда статуса должна отдавать один PDF-файл по всей карточке.

PDF должен содержать таблицу на каждый отображаемый блок:

- Заголовок блока.
- Таблица с колонками: `Название`, `Статус`.
- В `Статус` включать текущее значение, если оно есть: например `Готово: Хедсет`, `В процессе`, `Не заполнено`, `Не требуется`.

Правила цвета строк:

- Нежно-зеленый: поле готово.
- Нежно-красный: поле не готово и дедлайн прошел.
- Желтый: поле не готово и дедлайн сегодня.
- Без яркой заливки/нейтральный: дедлайн в будущем или поле опционально.

Пустые `type: multiple` блоки и незаполненные `type: optional` блоки не показывать вообще. Например, если нет добавленных объявлений, блок `Объявления` не должен попадать в PDF и не должен подсвечиваться.

PDF должен корректно отображать кириллицу. В Docker установить системный TTF-шрифт, например `fonts-dejavu-core`, и регистрировать его в ReportLab через `TTFont`. Не использовать PDF core fonts для русского текста.

## Доступы и безопасность

Роли:

- `viewer` — может смотреть текущий статус, историю и задавать вопросы по карточке.
- `editor` — все права viewer + может предлагать изменения карточки и подтверждать свои pending changes.
- `admin` — root-admin из конфига. Может одобрять/отклонять пользователей, выдавать `viewer`/`editor`, блокировать пользователей и чаты, смотреть audit.

Новый пользователь:

1. Пишет боту `/start` или любое сообщение.
2. Если его нет в базе — создать `pending` пользователя.
3. Уведомить `admin_user_id` с кнопками:
   - `Approve viewer`
   - `Approve editor`
   - `Reject`
4. До одобрения не отправлять его сообщения в LLM.
5. После решения отправить пользователю уведомление: доступ одобрен/отклонен.

Чаты:

- Private chat обрабатывается по статусу пользователя.
- Group/supergroup всегда read-only.
- Неодобренные или заблокированные чаты не обрабатывать через LLM.
- Для групп разрешить только безопасные read-only команды: `/status`, `/summary`, `/help`, если чат не заблокирован и пользователь имеет доступ.
- В группе нельзя менять карточку, даже если пишет editor/admin.

Блокировки:

- Admin может заблокировать пользователя или чат.
- Заблокированные пользователи/чаты получают короткий ответ без LLM: `Доступ заблокирован. Обратитесь к администратору.`
- Все попытки заблокированных пользователей писать боту логировать без содержимого голосовых/текстов в LLM.

## Командный интерфейс

Сделать понятные команды. Все команды должны работать без LLM.

Базовые:

- `/start` — регистрация/статус доступа.
- `/help` — помощь по ролям и командам.
- `/whoami` — текущая роль и статус.
- `/status` — сформировать и отправить PDF текущей карточки.
- `/summary` — краткий текстовый статус: сколько готово, сколько просрочено, что нужно сегодня.
- `/history` — список последних карточек.
- `/history YYYY-WW` — PDF/summary карточки указанной недели.
- `/schema` — версия схемы и список блоков.

Для editor/admin в private chat:

- `/update` — начать кнопочный интерфейс обновления карточки.
- `/pending` — мои ожидающие подтверждения.
- `/cancel <pending_change_id>` — отменить pending change.

Для admin:

- `/users` — список pending/approved/blocked.
- `/approve <telegram_user_id> viewer|editor`
- `/reject <telegram_user_id>`
- `/block_user <telegram_user_id>`
- `/unblock_user <telegram_user_id>`
- `/block_chat <chat_id>`
- `/unblock_chat <chat_id>`
- `/audit [limit]`

Для удобства сделать inline keyboard там, где это лучше команд:

- выбор блока/экземпляра/поля/значения в `/update`;
- подтверждение proposed change: `✅ Применить` / `❌ Отменить`;
- одобрение пользователя: `Viewer` / `Editor` / `Reject`;
- быстрые действия в `/summary`: `Открыть PDF`, `Что просрочено`, `Что сегодня`.

## Natural language и voice интерфейс

В private chat для approved viewer/editor/admin:

1. Текстовые сообщения обрабатываются intent-классификатором.
2. Голосовые сообщения сначала транскрибируются через AITUNNEL STT, потом обрабатываются как текст.
3. Если пользователь задает вопрос — ответить по текущей карточке.
4. Если пользователь просит изменить карточку — построить patch, показать preview и кнопки `Применить`/`Отменить`.
5. Если недостаточно данных — задать уточняющий вопрос, не делать изменений.
6. Если пользователь viewer пытается изменить карточку — объяснить, что доступ read-only.

Примеры сообщений:

- `Кто главный спикер?`
- `Покажи что еще не готово на сегодня`
- `Поставь главному спикеру микрофон хедсет`
- `Добавь объявление про молодежный лагерь, слайды еще в процессе, QR не требуется`
- `В книге поставь название "Путь к свободе", слайд готов`

## LLM-интеграция

Использовать `AsyncOpenAI`:

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=config.llm.api_key,
    base_url=config.llm.base_url,
)
```

Для текста использовать Chat Completions или Responses API, но внутри приложения обернуть это в собственный `LlmClient`, чтобы можно было заменить API без переписывания бизнес-логики.

Обязательно использовать структурированный JSON-ответ для intent parsing. Не парсить свободный текст модели регулярками.

Минимальная JSON Schema ответа LLM:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["intent", "confidence", "answer", "patches", "needs_clarification", "clarification_question"],
  "properties": {
    "intent": {
      "type": "string",
      "enum": ["question", "show_status", "show_history", "propose_update", "unknown"]
    },
    "confidence": {"type": "number"},
    "answer": {"type": ["string", "null"]},
    "needs_clarification": {"type": "boolean"},
    "clarification_question": {"type": ["string", "null"]},
    "patches": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["op", "block_id", "entry_id", "field_id", "value", "human_label"],
        "properties": {
          "op": {"type": "string", "enum": ["set_field", "add_entry", "delete_entry"]},
          "block_id": {"type": "string"},
          "entry_id": {"type": ["string", "null"]},
          "field_id": {"type": ["string", "null"]},
          "value": {"type": ["string", "null"]},
          "human_label": {"type": "string"}
        }
      }
    }
  }
}
```

System prompt для LLM должен включать:

- актуальную YAML-схему в компактном виде;
- текущую карточку в JSON;
- роль пользователя;
- запрет выдумывать поля/блоки;
- запрет сразу применять изменения;
- требование возвращать только JSON по схеме;
- правило: если есть сомнение, ставить `needs_clarification=true`.

LLM не должен быть источником истины. Источник истины — SQLite + YAML-схема. Все patches после LLM нужно валидировать кодом:

- существует ли block_id;
- существует ли field_id;
- нужен ли entry_id для type=multiple блока;
- может ли роль пользователя менять данные;
- не заблокирован ли пользователь/чат;
- значение не пустое там, где это невозможно;
- patch не конфликтует с текущим состоянием.

## Голосовые сообщения

Flow:

1. Получить `voice.file_id` из Telegram.
2. Скачать `.ogg` во временную директорию.
3. Отправить файл в `client.audio.transcriptions.create(...)`.
4. Указать `language="ru"`.
5. Полученный текст прогнать через обычный text-flow.
6. В ответе пользователю показать распознанный текст коротко: `Я распознал: ...` и дальше preview/ответ.

Не хранить аудио постоянно. Удалять временный файл после обработки, кроме debug-режима.

## Рекомендованные модели в конфиге

Для `text_model`:

- качественный вариант: `claude-sonnet-4-5` или актуальный Claude Sonnet из панели AITUNNEL;
- сильный дорогой вариант: `gpt-5.2-pro`;
- более дешевый быстрый вариант: `google/gemini-2.5-flash` или актуальная flash-модель;
- обязательно проверить, что выбранная модель поддерживает structured outputs/tool calling в AITUNNEL.

Для `audio_model`:

- стартовый вариант для Telegram voice: `whisper-large-v3-turbo`;
- бюджетный массовый вариант: `qwen3-asr-flash-2026-02-10`;
- премиум точность: `gpt-4o-transcribe`;
- если нужны несколько говорящих в длинных записях: `gpt-4o-transcribe-diarize`.

Модели не хардкодить. Все model ids должны приходить из app config.

## Напоминания

Напоминания отправляются editor/admin за `notifications.remind_before_hours` до дедлайна.

Алгоритм:

1. Каждую минуту или по интервалу из конфига пройти по текущей карточке.
2. Найти поля, которые не ready и имеют deadline.
3. Вычислить deadline datetime внутри текущей недели.
4. Если текущее время попало в окно `[deadline - remind_before_hours, deadline)` и уведомление еще не отправлялось — отправить editor/admin.
5. Если дедлайн уже прошел, не слать бесконечные напоминания. Просрочка видна в `/summary` и PDF.

Текст напоминания должен быть коротким:

```text
Напоминание по собранию: через 3 часа дедлайн.
Блок: Главный спикер
Пункт: Слайды готовы
Текущий статус: В процессе
```

## PDF-генерация

Реализовать отдельный сервис `PdfReportBuilder`.

Требования:

- Один PDF-файл на весь статус.
- Несколько таблиц внутри PDF — по одной на блок.
- Кириллица работает в Docker и локально.
- Цвета мягкие, не кислотные.
- Длинный текст переносится по строкам.
- Название файла: `meeting_status_<week_start_date>.pdf`.
- PDF не должен включать пустые `multiple`-блоки и незаполненные `optional`-блоки.

Для ReportLab:

- зарегистрировать TTF font из `pdf.font_path`;
- если файла нет — попробовать известные системные пути;
- если шрифт не найден — завершить с понятной ошибкой, а не генерировать PDF с квадратиками.

## Архитектура проекта

Предпочтительная структура:

```text
.
├── AGENTS.md
├── README.md
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── config/
│   ├── app.example.yaml
│   └── service_schema.example.yaml
├── src/
│   └── meeting_bot/
│       ├── __main__.py
│       ├── config.py
│       ├── schema.py
│       ├── storage.py
│       ├── models.py
│       ├── card_service.py
│       ├── readiness.py
│       ├── deadlines.py
│       ├── pdf_report.py
│       ├── llm_client.py
│       ├── intent_parser.py
│       ├── voice.py
│       ├── access.py
│       ├── notifications.py
│       ├── bot.py
│       ├── handlers/
│       │   ├── commands.py
│       │   ├── callbacks.py
│       │   ├── messages.py
│       │   └── admin.py
│       └── utils.py
└── tests/
    ├── test_schema.py
    ├── test_readiness.py
    ├── test_deadlines.py
    ├── test_card_service.py
    ├── test_access.py
    ├── test_intent_parser.py
    └── test_pdf_report.py
```

## Ошибки и UX

Пользовательские ошибки должны быть понятными:

- `Я не нашел поле main_speaker.foo в текущей схеме.`
- `Этот блок повторяемый, нужно выбрать конкретный экземпляр.`
- `Я не уверен, к какому объявлению относится изменение. Уточни название объявления.`
- `В группах карточку менять нельзя. Напиши мне в личку.`
- `Ты пока не одобрен. Я отправил заявку администратору.`

Нельзя показывать пользователю stack trace.

## Логирование

Логировать:

- запуск и версию схемы;
- создание карточки недели;
- регистрации пользователей;
- approval/reject/block actions;
- proposed patches и результат применения;
- ошибки AITUNNEL без секретов;
- ошибки Telegram API без токена.

Не логировать:

- Telegram bot token;
- AITUNNEL API key;
- полный текст голосовых сообщений в debug=false;
- приватные персональные данные сверх необходимого.

## Тесты

Минимально обязательные тесты:

1. Парсинг YAML-схемы.
2. `type: multiple` пустой блок не попадает в статус/PDF.
3. Добавленный `multiple` entry попадает в статус/PDF.
4. `Не пусто` работает.
5. Placeholder `<Название>` считается готовым для непустого значения.
6. `Любое значение или его отсутствие` не подсвечивается красным.
7. Просроченное поле получает `overdue`.
8. Поле с дедлайном сегодня получает `due_today`.
9. Готовое поле получает зеленый статус.
10. Viewer не может создать patch.
11. Editor может создать pending patch, но данные не меняются до callback confirm.
12. Group chat не может менять карточку.
13. Pending/blocked users не отправляются в LLM.
14. PDF smoke test создает файл и не падает на кириллице.
15. LLM client тестируется через mock, без реальных запросов.

## Definition of Done

Фича считается готовой, когда:

- Бот стартует через Docker Compose.
- Принимает два файла: app config и meeting schema.
- Создает текущую карточку недели.
- `/status` отправляет один PDF с корректной кириллицей.
- Пустые repeatable-блоки не отображаются.
- Добавленные repeatable-блоки отображаются и подсвечиваются.
- Новый пользователь проходит approval flow.
- Pending/blocked users/chats не вызывают LLM.
- Группы read-only.
- Editor/admin могут предложить изменение текстом или голосом.
- Изменение применяется только после inline-confirm.
- Архив прошлых недель хранится в SQLite.
- Есть тесты core-логики.
- README содержит запуск, конфиг, команды и модель данных.

## Важные ограничения

- Не реализовывать мульти-собрания.
- Не делать web UI.
- Не использовать PostgreSQL.
- Не применять изменения без подтверждения пользователя.
- Не отправлять неодобренные/заблокированные сообщения в LLM.
- Не хардкодить текущие блоки YAML-схемы.
- Не ломать старые карточки при изменении схемы.
