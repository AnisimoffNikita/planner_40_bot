import json
from types import SimpleNamespace
from typing import cast

import pytest
from openai import AsyncOpenAI

from meeting_bot.config import LlmConfig
from meeting_bot.intent_parser import INTENT_JSON_SCHEMA
from meeting_bot.llm_client import (
    LlmClient,
    LlmUnavailable,
    build_intent_context,
    build_system_prompt,
)


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class FakeClient:
    def __init__(self, content: str) -> None:
        self.fake_completions = FakeCompletions(content)
        self.chat = SimpleNamespace(completions=self.fake_completions)
        self.audio = SimpleNamespace()


class FakeBadRequest(Exception):
    status_code = 400


class FallbackCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise FakeBadRequest("response_format json_schema is not supported")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(function=SimpleNamespace(arguments=self.content))
                        ],
                    )
                )
            ]
        )


class FallbackClient:
    def __init__(self, content: str) -> None:
        self.fake_completions = FallbackCompletions(content)
        self.chat = SimpleNamespace(completions=self.fake_completions)
        self.audio = SimpleNamespace()


def test_intent_json_schema_is_strict_provider_friendly() -> None:
    schema = INTENT_JSON_SCHEMA["schema"]
    patch_schema = schema["properties"]["patches"]["items"]

    assert "$defs" not in schema
    assert set(schema["required"]) == set(schema["properties"])
    assert set(patch_schema["required"]) == set(patch_schema["properties"])
    assert patch_schema["properties"]["entry_id"]["type"] == ["string", "null"]


def test_intent_context_contains_schema_and_repeatable_entries(loaded_schema) -> None:
    context = build_intent_context(
        loaded_schema.schema,
        {
            "blocks": {
                "speaker": {"fields": {"slides": "В процессе", "unknown": "old"}},
                "announcements": [
                    {
                        "entry_id": "entry-1",
                        "title": "Лагерь",
                        "fields": {
                            "title": "Лагерь",
                            "approved": "Да",
                            "legacy": "hidden",
                        },
                    }
                ],
            }
        },
    )

    speaker = next(block for block in context["blocks"] if block["id"] == "speaker")
    assert speaker["title"] == "Спикер"
    assert speaker["type"] == "required"
    assert "multiple" not in speaker
    assert speaker["default_field_when_omitted"] == "name"
    assert speaker["current_fields"] == {"slides": "В процессе"}
    assert {field["id"] for field in speaker["fields"]} == {
        "name",
        "slides",
        "props",
        "notes",
    }

    announcements = next(block for block in context["blocks"] if block["id"] == "announcements")
    assert announcements["type"] == "multiple"
    assert "multiple" not in announcements
    assert announcements["entries"] == [
        {
            "entry_id": "entry-1",
            "title": "Лагерь",
            "fields": {"title": "Лагерь", "approved": "Да"},
        }
    ]


def test_system_prompt_is_schema_driven_without_hardcoded_blocks(loaded_schema) -> None:
    prompt = build_system_prompt(
        schema=loaded_schema.schema,
        card_data={"blocks": {}},
        role="editor",
    )

    assert "set_field" in prompt
    assert "clear_field" in prompt
    assert "clear_block" in prompt
    assert "add_entry" in prompt
    assert "delete_entry" in prompt
    assert "Спикер Новое значение" in prompt
    assert "speaker.name" in prompt
    assert "announcements" in prompt
    assert "main_speaker" not in prompt


async def test_structured_intent_is_validated(loaded_schema) -> None:
    content = (
        '{"intent":"question","confidence":0.9,"answer":"Иван",'
        '"patches":[],"needs_clarification":false,"clarification_question":null}'
    )
    fake = FakeClient(content)
    client = LlmClient(
        LlmConfig(enabled=True, api_key="test"),
        cast(AsyncOpenAI, fake),
    )
    result = await client.parse(
        text="Кто спикер?",
        schema=loaded_schema.schema,
        card_data={"blocks": {}},
        role="viewer",
    )
    assert result.answer == "Иван"
    assert fake.fake_completions.kwargs["response_format"]["type"] == "json_schema"


async def test_json_schema_bad_request_falls_back_to_tool_call(loaded_schema) -> None:
    content = {
        "intent": "propose_update",
        "confidence": 0.95,
        "answer": None,
        "patches": [
            {
                "op": "set_field",
                "block_id": "speaker",
                "entry_id": None,
                "field_id": "name",
                "value": "Иван Иванов",
                "human_label": "Спикер — Имя",
            }
        ],
        "needs_clarification": False,
        "clarification_question": None,
    }
    fake = FallbackClient(json.dumps(content, ensure_ascii=False))
    client = LlmClient(
        LlmConfig(enabled=True, api_key="test"),
        cast(AsyncOpenAI, fake),
    )

    result = await client.parse(
        text="измени имя спикера на Иван Иванов",
        schema=loaded_schema.schema,
        card_data={"blocks": {}},
        role="editor",
    )

    assert result.patches[0].value == "Иван Иванов"
    calls = fake.fake_completions.calls
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[1]["tools"][0]["function"]["name"] == "meeting_intent"
    assert "response_format" not in calls[1]


@pytest.mark.parametrize(
    ("text", "content", "expected"),
    [
        (
            "спикер Иван Иванов",
            {
                "intent": "propose_update",
                "confidence": 0.95,
                "answer": None,
                "patches": [
                    {
                        "op": "set_field",
                        "block_id": "speaker",
                        "entry_id": None,
                        "field_id": "name",
                        "value": "Иван Иванов",
                        "human_label": "Спикер — Имя",
                    }
                ],
                "needs_clarification": False,
                "clarification_question": None,
            },
            ("set_field", "speaker", None, "name", "Иван Иванов"),
        ),
        (
            "слайды спикера да",
            {
                "intent": "propose_update",
                "confidence": 0.93,
                "answer": None,
                "patches": [
                    {
                        "op": "set_field",
                        "block_id": "speaker",
                        "entry_id": None,
                        "field_id": "slides",
                        "value": "Да",
                        "human_label": "Спикер — Слайды",
                    }
                ],
                "needs_clarification": False,
                "clarification_question": None,
            },
            ("set_field", "speaker", None, "slides", "Да"),
        ),
        (
            "добавь объявление лагерь, согласовано в процессе",
            {
                "intent": "propose_update",
                "confidence": 0.9,
                "answer": None,
                "patches": [
                    {
                        "op": "add_entry",
                        "block_id": "announcements",
                        "entry_id": "new-announcement",
                        "field_id": None,
                        "value": "Лагерь",
                        "human_label": "Лагерь",
                    },
                    {
                        "op": "set_field",
                        "block_id": "announcements",
                        "entry_id": "new-announcement",
                        "field_id": "approved",
                        "value": "В процессе",
                        "human_label": "Лагерь — Согласовано",
                    },
                ],
                "needs_clarification": False,
                "clarification_question": None,
            },
            ("add_entry", "announcements", "new-announcement", None, "Лагерь"),
        ),
        (
            "удали объявление лагерь",
            {
                "intent": "propose_update",
                "confidence": 0.91,
                "answer": None,
                "patches": [
                    {
                        "op": "delete_entry",
                        "block_id": "announcements",
                        "entry_id": "entry-1",
                        "field_id": None,
                        "value": None,
                        "human_label": "Лагерь",
                    }
                ],
                "needs_clarification": False,
                "clarification_question": None,
            },
            ("delete_entry", "announcements", "entry-1", None, None),
        ),
    ],
)
async def test_update_patch_shapes_from_mock_llm_are_validated(
    loaded_schema,
    text: str,
    content: dict[str, object],
    expected: tuple[str, str, str | None, str | None, str | None],
) -> None:
    fake = FakeClient(json.dumps(content, ensure_ascii=False))
    client = LlmClient(
        LlmConfig(enabled=True, api_key="test"),
        cast(AsyncOpenAI, fake),
    )
    result = await client.parse(
        text=text,
        schema=loaded_schema.schema,
        card_data={
            "blocks": {
                "announcements": [
                    {"entry_id": "entry-1", "title": "Лагерь", "fields": {"title": "Лагерь"}}
                ]
            }
        },
        role="editor",
    )

    patch = result.patches[0]
    assert (patch.op, patch.block_id, patch.entry_id, patch.field_id, patch.value) == expected


async def test_ambiguous_repeatable_update_can_ask_clarification(loaded_schema) -> None:
    content = {
        "intent": "propose_update",
        "confidence": 0.8,
        "answer": None,
        "patches": [],
        "needs_clarification": True,
        "clarification_question": "Какое объявление изменить?",
    }
    client = LlmClient(
        LlmConfig(enabled=True, api_key="test"),
        cast(AsyncOpenAI, FakeClient(json.dumps(content, ensure_ascii=False))),
    )

    result = await client.parse(
        text="поставь объявлению согласовано да",
        schema=loaded_schema.schema,
        card_data={
            "blocks": {
                "announcements": [
                    {"entry_id": "entry-1", "title": "Лагерь", "fields": {"title": "Лагерь"}},
                    {"entry_id": "entry-2", "title": "Книга", "fields": {"title": "Книга"}},
                ]
            }
        },
        role="editor",
    )

    assert result.needs_clarification is True
    assert result.clarification_question == "Какое объявление изменить?"


async def test_invalid_output_is_wrapped(loaded_schema) -> None:
    client = LlmClient(
        LlmConfig(enabled=True, api_key="test"),
        cast(AsyncOpenAI, FakeClient("not-json")),
    )
    with pytest.raises(LlmUnavailable):
        await client.parse(
            text="test",
            schema=loaded_schema.schema,
            card_data={},
            role="viewer",
        )
