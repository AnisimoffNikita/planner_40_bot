from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError

from meeting_bot.config import LlmConfig
from meeting_bot.domain import IntentResult
from meeting_bot.intent_parser import (
    INTENT_JSON_SCHEMA,
    PATCH_OPERATION_VALUES,
    validate_intent_domain,
)
from meeting_bot.schema import BlockSpec, MeetingSchema

logger = logging.getLogger(__name__)

DEFAULT_FIELD_IDS = {"name", "title", "topic"}
DEFAULT_FIELD_LABELS = {"имя", "название", "тема", "заголовок"}


class LlmUnavailable(RuntimeError):
    pass


def build_intent_context(schema: MeetingSchema, card_data: dict[str, Any]) -> dict[str, Any]:
    data_blocks = card_data.get("blocks", {})
    if not isinstance(data_blocks, dict):
        data_blocks = {}
    return {
        "schema_version": schema.version,
        "schema_title": schema.title,
        "patch_operations": PATCH_OPERATION_VALUES,
        "blocks": [_block_context(block, data_blocks.get(block.id)) for block in schema.blocks],
    }


def build_system_prompt(
    *,
    schema: MeetingSchema,
    card_data: dict[str, Any],
    role: str,
) -> str:
    context = build_intent_context(schema, card_data)
    examples = _example_lines(schema)
    return (
        "Ты intent parser и безопасный редактор данных Telegram-бота подготовки одного "
        "еженедельного собрания. Верни только JSON по заданной JSON Schema. "
        "Источник истины: YAML-схема и текущая карточка из контекста; не выдумывай "
        "block_id, field_id или entry_id. Никогда не применяй изменения сам: для правок "
        "верни intent=propose_update и patches, приложение покажет preview и попросит "
        "подтверждение. "
        "Блоки бывают type=required (один обязательный), type=optional (ноль или один) "
        "и type=multiple (ноль или много entries). Поддержанные операции patches: "
        "set_field, clear_field, clear_block, add_entry, delete_entry. Для set_field "
        "укажи block_id, field_id, value; для clear_field block_id и field_id; для "
        "clear_block только required/optional block_id; для add_entry только "
        "type=multiple block_id и value/title; для delete_entry только type=multiple "
        "block_id и существующий entry_id. "
        "Понимай общие формулировки: 'поставь/измени/обнови <поле> <блока> на <значение>' "
        "как set_field, 'очисти/убери значение <поле>' как clear_field, 'очисти блок "
        "<блок>' как clear_block, 'добавь <multiple-блок> ...' как add_entry плюс "
        "set_field для известных деталей, 'удали <multiple-блок/entry>' как delete_entry. "
        "Если пользователь называет блок и значение без поля, используй "
        "default_field_when_omitted только когда он задан в контексте и цель однозначна; "
        "иначе needs_clarification=true. Для type=multiple блоков изменение или удаление "
        "существующего элемента требует однозначного entry_id из entries. При добавлении "
        "нового multiple-элемента можно сначала вернуть add_entry, а последующие "
        "set_field привязать к нему тем же entry_id или оставить entry_id пустым, если в "
        "этом patch ровно один add_entry для блока. "
        "Если есть сомнение в блоке, поле, entry или значении, не угадывай: "
        "needs_clarification=true и задай короткий clarification_question. "
        f"Роль пользователя: {role}. "
        "Формат JSON-ответа: "
        f"{json.dumps(INTENT_JSON_SCHEMA['schema'], ensure_ascii=False, separators=(',', ':'))}. "
        f"Примеры по текущей схеме: {examples}. "
        "Контекст схемы и текущей карточки: "
        f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}."
    )


def _block_context(block: BlockSpec, stored: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": block.id,
        "title": block.title,
        "type": block.type.value,
        "default_field_when_omitted": _default_field_when_omitted(block),
        "fields": [
            {
                "id": field_id,
                "label": field.label,
                "allowed_values": field.allowed_values,
                "ready_if": field.ready_if,
                "deadline": field.deadline.model_dump() if field.deadline is not None else None,
            }
            for field_id, field in block.fields.items()
        ],
    }
    if block.is_multiple:
        item["entries"] = _repeatable_entries(block, stored)
    else:
        fields = stored.get("fields", {}) if isinstance(stored, dict) else {}
        item["current_fields"] = _known_fields(block, fields)
    return item


def _repeatable_entries(block: BlockSpec, stored: Any) -> list[dict[str, Any]]:
    if not isinstance(stored, list):
        return []
    entries: list[dict[str, Any]] = []
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        fields = entry.get("fields", {})
        title = entry.get("title")
        entries.append(
            {
                "entry_id": str(entry.get("entry_id", "")),
                "title": title if isinstance(title, str) else "",
                "fields": _known_fields(block, fields),
            }
        )
    return entries


def _known_fields(block: BlockSpec, fields: Any) -> dict[str, Any]:
    if not isinstance(fields, dict):
        return {}
    return {field_id: value for field_id, value in fields.items() if field_id in block.fields}


def _default_field_when_omitted(block: BlockSpec) -> str | None:
    candidates = [
        field_id
        for field_id, field in block.fields.items()
        if field_id.casefold() in DEFAULT_FIELD_IDS
        or field.label.strip().casefold() in DEFAULT_FIELD_LABELS
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _example_lines(schema: MeetingSchema) -> list[str]:
    examples: list[str] = []
    singleton = next(
        (
            block
            for block in schema.blocks
            if block.is_singleton and _default_field_when_omitted(block)
        ),
        None,
    )
    if singleton is not None:
        field_id = _default_field_when_omitted(singleton)
        if field_id is not None:
            field = singleton.fields[field_id]
            examples.append(
                f"'{singleton.title} Новое значение' -> set_field "
                f"{singleton.id}.{field_id} ({field.label})"
            )
    field_example = _first_non_default_singleton_field(schema)
    if field_example is not None:
        block, field_id = field_example
        field = block.fields[field_id]
        examples.append(
            f"'{field.label} {block.title} Новое значение' -> set_field {block.id}.{field_id}"
        )
    repeatable = next((block for block in schema.blocks if block.is_multiple), None)
    if repeatable is not None:
        examples.append(f"'добавь {repeatable.title} Новая запись' -> add_entry {repeatable.id}")
        examples.append(
            f"'удали {repeatable.title} Новая запись' -> delete_entry {repeatable.id} "
            "только если entries содержит однозначный entry_id"
        )
    return examples


def _first_non_default_singleton_field(schema: MeetingSchema) -> tuple[BlockSpec, str] | None:
    for block in schema.blocks:
        if block.is_multiple:
            continue
        default_field = _default_field_when_omitted(block)
        for field_id in block.fields:
            if field_id != default_field:
                return block, field_id
    return None


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
        system = build_system_prompt(
            schema=schema,
            card_data=card_data,
            role=role,
        )
        user_text = text
        if clarification_context:
            user_text = f"Контекст предыдущего уточнения: {clarification_context}\nОтвет: {text}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        try:
            content = await self._complete_intent(messages)
            result = IntentResult.model_validate_json(content)
            return validate_intent_domain(result, schema)
        except (ValidationError, ValueError) as exc:
            raise LlmUnavailable(
                "Модель вернула изменение, которое не проходит проверку схемы. "
                "Уточни блок, поле или элемент."
            ) from exc
        except LlmUnavailable:
            raise
        except Exception as exc:
            raise LlmUnavailable("Не удалось обработать запрос через LLM.") from exc

    async def _complete_intent(self, messages: list[dict[str, str]]) -> str:
        try:
            return await self._request_intent(
                messages,
                response_format={"type": "json_schema", "json_schema": INTENT_JSON_SCHEMA},
            )
        except Exception as exc:
            if not _is_bad_request(exc):
                raise
            logger.warning(
                "LLM provider rejected json_schema response format; retrying with tool call: %s",
                _safe_error(exc),
            )

        try:
            return await self._request_intent(
                messages,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": INTENT_JSON_SCHEMA["name"],
                            "description": "Parse the Telegram message into a meeting bot intent.",
                            "parameters": INTENT_JSON_SCHEMA["schema"],
                        },
                    }
                ],
                tool_choice={
                    "type": "function",
                    "function": {"name": INTENT_JSON_SCHEMA["name"]},
                },
            )
        except Exception as exc:
            if not _is_bad_request(exc):
                raise
            logger.warning(
                "LLM provider rejected tool-call response format; retrying with JSON object: %s",
                _safe_error(exc),
            )

        return await self._request_intent(
            messages,
            response_format={"type": "json_object"},
        )

    async def _request_intent(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        response = await self.client.chat.completions.create(
            model=self.config.text_model,
            messages=messages,
            **kwargs,
        )
        content = _extract_message_content(response)
        if not content:
            raise LlmUnavailable("Модель вернула пустой ответ.")
        return content

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


def _extract_message_content(response: Any) -> str | None:
    message = response.choices[0].message
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        function = getattr(tool_calls[0], "function", None)
        arguments = getattr(function, "arguments", None)
        if isinstance(arguments, str) and arguments.strip():
            return arguments
    return None


def _is_bad_request(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 400


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    if len(text) > 300:
        return text[:297] + "..."
    return text
