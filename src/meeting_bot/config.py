from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class TelegramConfig(BaseModel):
    token: SecretStr
    admin_user_id: int


class AppSection(BaseModel):
    timezone: str = "Europe/Moscow"
    database_path: Path = Path("./data/meeting_bot.sqlite3")
    log_level: str = "INFO"
    default_parse_mode: Literal["HTML", "Markdown", "MarkdownV2", None] = "HTML"
    debug: bool = False

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value


class MeetingConfig(BaseModel):
    week_starts_on: int = Field(default=1, ge=1, le=7)
    service_day: int = Field(default=7, ge=1, le=7)
    archive_after_service: bool = True


class NotificationsConfig(BaseModel):
    enabled: bool = True
    remind_before_hours: int = Field(default=3, ge=1, le=168)
    scheduler_interval_seconds: int = Field(default=60, ge=15, le=86400)


class LlmConfig(BaseModel):
    enabled: bool = True
    base_url: str = "https://api.aitunnel.ru/v1/"
    api_key: SecretStr | None = None
    text_model: str = "gpt-4o"
    audio_model: str = "whisper-large-v3-turbo"
    request_timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_retries: int = Field(default=3, ge=0, le=10)
    max_voice_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)

    @model_validator(mode="after")
    def require_key_when_enabled(self) -> LlmConfig:
        if self.enabled and self.api_key is None:
            raise ValueError("llm.api_key is required when llm.enabled=true")
        return self


class PdfConfig(BaseModel):
    font_path: Path | None = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    output_dir: Path = Path("./data/reports")


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram: TelegramConfig
    app: AppSection = Field(default_factory=AppSection)
    meeting: MeetingConfig = Field(default_factory=MeetingConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    pdf: PdfConfig = Field(default_factory=PdfConfig)
    source_path: Path = Field(exclude=True)

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app.timezone)


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} is not set")
        return os.environ[name]

    return ENV_PATTERN.sub(replace, value)


def _resolve_path(path: Path | None, base_dir: Path) -> Path | None:
    if path is None or path.is_absolute():
        return path
    return (base_dir / path).resolve()


def load_app_config(path: str | Path) -> AppConfig:
    source_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load app config {source_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("App config must be a YAML object")
    raw = _expand_env(raw)
    raw["source_path"] = source_path
    config = AppConfig.model_validate(raw)
    base_dir = source_path.parent
    config.app.database_path = _resolve_path(config.app.database_path, base_dir)  # type: ignore[assignment]
    config.pdf.output_dir = _resolve_path(config.pdf.output_dir, base_dir)  # type: ignore[assignment]
    config.pdf.font_path = _resolve_path(config.pdf.font_path, base_dir)
    return config
