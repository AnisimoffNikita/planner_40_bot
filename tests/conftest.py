from __future__ import annotations

from pathlib import Path

import pytest

from meeting_bot.card_service import CardService
from meeting_bot.config import (
    AppConfig,
    AppSection,
    LlmConfig,
    MeetingConfig,
    NotificationsConfig,
    PdfConfig,
    TelegramConfig,
)
from meeting_bot.schema import load_meeting_schema
from meeting_bot.storage import Database


@pytest.fixture
def schema_path(tmp_path: Path) -> Path:
    path = tmp_path / "schema.yaml"
    path.write_text(
        """
version: "1.0"
title: "Тестовое собрание"
blocks:
  - id: speaker
    title: "Спикер"
    multiple: false
    fields:
      name:
        label: "Имя"
        allowed_values: ["Имя Фамилия"]
        ready_if: ["Не пусто"]
        deadline: {day: 3, hour: 10, minute: 0}
      slides:
        label: "Слайды"
        allowed_values: ["Да", "В процессе"]
        ready_if: ["Да"]
        deadline: {day: 7, hour: 12, minute: 0}
      props:
        label: "Реквизит"
        allowed_values: ["<Название>", "Не требуется"]
        ready_if: ["<Название>", "Не требуется"]
        deadline: {day: 6, hour: 10, minute: 0}
      notes:
        label: "Пометки"
        allowed_values: ["Любое значение"]
        ready_if: ["Любое значение или его отсутствие"]
        deadline: null
  - id: announcements
    title: "Объявления"
    multiple: true
    fields:
      title:
        label: "Название"
        allowed_values: ["Строка"]
        ready_if: ["Не пусто"]
        deadline: {day: 4, hour: 17, minute: 0}
      approved:
        label: "Согласовано"
        allowed_values: ["Да", "В процессе", "Не требуется"]
        ready_if: ["Да", "Не требуется"]
        deadline: {day: 7, hour: 15, minute: 0}
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="123:test", admin_user_id=1),
        app=AppSection(
            timezone="Europe/Moscow",
            database_path=tmp_path / "bot.sqlite3",
            log_level="INFO",
            default_parse_mode="HTML",
        ),
        meeting=MeetingConfig(week_starts_on=1, service_day=7),
        notifications=NotificationsConfig(
            enabled=True, remind_before_hours=3, scheduler_interval_seconds=60
        ),
        llm=LlmConfig(enabled=False),
        pdf=PdfConfig(
            font_path=Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
            output_dir=tmp_path / "reports",
        ),
        source_path=tmp_path / "app.yaml",
    )


@pytest.fixture
async def database(app_config: AppConfig) -> Database:
    database = Database(app_config.app.database_path)
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def loaded_schema(schema_path: Path):
    return load_meeting_schema(schema_path)


@pytest.fixture
def card_service(database: Database, app_config: AppConfig, loaded_schema):
    return CardService(database, app_config, loaded_schema)
