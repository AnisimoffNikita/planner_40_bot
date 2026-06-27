# Meeting Bot

Production-ready Telegram-бот для подготовки одного еженедельного собрания по
произвольной YAML-схеме. Источник истины — SQLite и переданный при запуске файл
схемы; LLM только разбирает естественный язык и никогда не применяет изменения
самостоятельно.

## Быстрый старт

Требуется Python 3.12+.

```bash
cp config/app.example.yaml config/app.yaml
export TELEGRAM_BOT_TOKEN='123:token'
export AITUNNEL_API_KEY='sk-aitunnel-...'
uv sync --extra dev
uv run python -m meeting_bot \
  --app-config ./config/app.yaml \
  --meeting-schema ./service_schema.yaml
```

`telegram.admin_user_id` — единственный root-admin. После старта ему необходимо
первым написать боту в личные сообщения: Telegram не позволяет боту инициировать
диалог с пользователем.

Относительные `database_path`, `pdf.output_dir` и `pdf.font_path` разрешаются
относительно каталога app-конфига. В строках YAML поддерживаются `${ENV_VAR}`.

## Docker Compose

```bash
cp config/app.example.yaml config/app.yaml
export TELEGRAM_BOT_TOKEN='123:token'
export AITUNNEL_API_KEY='sk-aitunnel-...'
docker compose up --build
```

Compose монтирует `config/app.yaml`, `service_schema.yaml` и volume `meeting-data`
для SQLite/PDF. Контейнер работает от непривилегированного пользователя и
содержит DejaVu Sans для кириллицы.

Для существующей базы перед обновлением можно явно применить миграции:

```bash
MEETING_BOT_DATABASE_URL=sqlite:////app/data/meeting_bot.sqlite3 \
  alembic upgrade head
```

## Схема и карточки

Блоки описываются полем `type`: `required` — один обязательный блок,
`optional` — ноль или один блок, `multiple` — ноль или много записей. Значения
хранятся в JSON:

```json
{
  "blocks": {
    "main_speaker": {"fields": {"name": "Иван Иванов"}},
    "communion": null,
    "announcements": [
      {
        "entry_id": "uuid",
        "title": "Молодежный лагерь",
        "fields": {"topic": "Молодежный лагерь"}
      }
    ]
  }
}
```

При изменении YAML текущая карточка привязывается к новому snapshot схемы:
новые поля появляются автоматически, удаленные значения остаются в JSON, но не
показываются. Архивная карточка сохраняет исходные `schema_version/schema_hash`.
Новая неделя создается при первом обращении или scheduler tick; предыдущая
карточка архивируется, а старые pending changes истекают.

SQLite работает с `foreign_keys=ON`, WAL и `busy_timeout`. Для резервной копии
при остановленном контейнере достаточно скопировать файл из volume. Для горячей
копии используйте SQLite backup API или команду `.backup`.

## Доступ

- `viewer`: статус, архив и вопросы по карточке;
- `editor`: права viewer и предложения изменений в личке;
- `admin`: root-admin из конфига, управление доступом и audit.

Новый пользователь создается как `pending`; root-admin получает inline-кнопки
Viewer/Editor/Reject. До approval и после block ни текст, ни голос не отправляются
в AITUNNEL. Группы всегда read-only и принимают `/status`, `/summary`, `/help`,
`/ask вопрос`, а также обращения вида `@planner40bot вопрос`, если Telegram
доставляет такие сообщения боту. При включенном BotFather Privacy Mode надежнее
использовать `/ask`, потому что slash-команды доставляются всегда.

## Команды

- `/start`, `/help`, `/whoami`
- `/status`, `/summary`, `/ask ТЕКСТ`, `/history [YYYY-WW]`, `/schema`
- `/update` — кнопочный интерфейс обновления карточки
- `/pending`, `/cancel ID`
- admin: `/users`, `/approve ID viewer|editor`, `/reject ID`,
  `/block_user ID`, `/unblock_user ID`, `/block_chat ID`,
  `/unblock_chat ID`, `/audit [limit]`

Кнопочные, natural-language и voice изменения всегда создают `pending_changes`.
Карточка меняется только после кнопки `✅ Применить`. Подтверждение проверяет
автора, срок жизни 24 часа, неделю и revision карточки.

## LLM и voice

Используется `AsyncOpenAI` с `base_url=https://api.aitunnel.ru/v1/`.
Intent parser запрашивает strict JSON Schema и затем повторно валидирует ответ
Pydantic и доменной логикой. Voice `.ogg` скачивается во временный файл,
транскрибируется с `language="ru"` и гарантированно удаляется.

Если `llm.enabled=false` либо API временно недоступен, все команды продолжают
работать. Model IDs полностью задаются конфигом.

## Проверка

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run mypy src/meeting_bot
docker compose config
docker compose --profile test build
docker compose --profile test run --rm meeting-bot-test
```

Автотесты не используют реальные Telegram/AITUNNEL токены. Ручной smoke test:

1. Запустить бота с реальными ключами и написать `/start` от root-admin.
2. Написать от нового пользователя, одобрить его как editor.
3. Проверить `/update`, отмену и применение preview.
4. Отправить вопрос и voice в личке.
5. Добавить бота в группу и убедиться, что изменение запрещено, а `/summary`
   работает.
6. Открыть `/status` и проверить кириллицу, цвета и отсутствие пустых multiple
   блоков.
