from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from meeting_bot.config import LlmConfig
from meeting_bot.domain import IntentResult
from meeting_bot.intent_parser import INTENT_JSON_SCHEMA, validate_intent_domain
from meeting_bot.schema import MeetingSchema


class LlmUnavailable(RuntimeError):
    pass


class LlmClient:
    def __init__(self, config: LlmConfig, client: AsyncOpenAI | None = None) -> None:
        self.config = config
        self.client = client
        if self.client is None and config.enabled:
            self.client = AsyncOpenAI(
                api_key=config.api_key.get_secret_value() if config.api_key else "",
                base_url=config.base_url,
                timeout=config.request_timeout_seconds,
                max_retries=config.max_retries,
            )

    async def parse(
        self,
        *,
        text: str,
        schema: MeetingSchema,
        card_data: dict[str, Any],
        role: str,
        clarification_context: str | None = None,
    ) -> IntentResult:
        if not self.config.enabled or self.client is None:
            raise LlmUnavailable("LLM отключен в конфигурации.")
        compact_schema = schema.compact_dict()
        system = (
            "Ты анализатор команд Telegram-бота подготовки одного еженедельного собрания. "
            "Верни только JSON по заданной JSON Schema. Не выдумывай block_id/field_id. "
            "Никогда не применяй изменения: только предложи patches. Если ссылка на repeatable "
            "экземпляр неоднозначна или данных недостаточно, needs_clarification=true. "
            f"Роль пользователя: {role}. Схема: "
            f"{json.dumps(compact_schema, ensure_ascii=False, separators=(',', ':'))}. "
            f"Текущая карточка: {json.dumps(card_data, ensure_ascii=False, separators=(',', ':'))}."
        )
        user_text = text
        if clarification_context:
            user_text = f"Контекст предыдущего уточнения: {clarification_context}\nОтвет: {text}"
        try:
            response = await self.client.chat.completions.create(
                model=self.config.text_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
                response_format={"type": "json_schema", "json_schema": INTENT_JSON_SCHEMA},
            )
            content = response.choices[0].message.content
            if not content:
                raise LlmUnavailable("Модель вернула пустой ответ.")
            result = IntentResult.model_validate_json(content)
            return validate_intent_domain(result, schema)
        except LlmUnavailable:
            raise
        except Exception as exc:
            raise LlmUnavailable("Не удалось обработать запрос через LLM.") from exc

    async def transcribe(self, file_path: str) -> str:
        if not self.config.enabled or self.client is None:
            raise LlmUnavailable("Распознавание речи отключено.")
        try:
            with open(file_path, "rb") as audio_file:
                result = await self.client.audio.transcriptions.create(
                    model=self.config.audio_model,
                    file=audio_file,
                    language="ru",
                )
            text = result.text.strip()
            if not text:
                raise LlmUnavailable("Не удалось распознать речь.")
            return text
        except LlmUnavailable:
            raise
        except Exception as exc:
            raise LlmUnavailable("Ошибка распознавания голосового сообщения.") from exc
