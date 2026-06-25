from types import SimpleNamespace
from typing import cast

import pytest
from openai import AsyncOpenAI

from meeting_bot.config import LlmConfig
from meeting_bot.llm_client import LlmClient, LlmUnavailable


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
