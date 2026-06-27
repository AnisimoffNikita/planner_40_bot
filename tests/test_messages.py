from __future__ import annotations

from types import SimpleNamespace

from telegram.constants import ChatAction

from meeting_bot.access import AccessContext, AccessService
from meeting_bot.domain import IntentResult, PatchOperation
from meeting_bot.handlers import messages


class FakeMessage:
    def __init__(self, text: str | None = None, voice: object | None = None) -> None:
        self.text = text
        self.voice = voice
        self.replies: list[tuple[str, dict[str, object]]] = []
        self.documents: list[object] = []

    async def reply_text(self, text: str, **kwargs: object) -> object:
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=100 + len(self.replies))

    async def reply_document(self, document: object, **kwargs: object) -> None:
        self.documents.append((document, kwargs))


class FakeLlm:
    def __init__(self, result: IntentResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> IntentResult:
        self.calls.append(kwargs)
        return self.result


class FakeClarifications:
    async def consume(self, user_id: int, chat_id: int) -> object | None:
        return None

    async def save(self, *args: object, **kwargs: object) -> None:
        return None


class FakeUpdateWizard:
    async def handle_text(self, user_id: int, chat_id: int, text: str) -> object | None:
        return None


class FakeVoice:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[object] = []

    async def transcribe_telegram_voice(self, voice: object, telegram_file: object) -> str:
        self.calls.append((voice, telegram_file))
        return self.text


class FakeBot:
    def __init__(self, username: str = "Planner40Bot") -> None:
        self.username = username
        self.messages: list[tuple[int, str]] = []
        self.chat_actions: list[tuple[int, ChatAction]] = []

    async def get_file(self, file_id: str) -> object:
        return SimpleNamespace(file_id=file_id)

    async def get_me(self) -> object:
        return SimpleNamespace(username=self.username)

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.messages.append((int(args[0]), str(args[1])))

    async def send_chat_action(self, *, chat_id: int, action: ChatAction) -> None:
        self.chat_actions.append((chat_id, action))


class FakeServices:
    def __init__(
        self,
        *,
        config: object | None = None,
        access: AccessService,
        cards: object,
        loaded_schema: object,
        llm: FakeLlm,
        voice: FakeVoice | None = None,
    ) -> None:
        self.config = config
        self.access = access
        self.cards = cards
        self.loaded_schema = loaded_schema
        self.llm = llm
        self.voice = voice
        self.clarifications = FakeClarifications()
        self.update_wizard = FakeUpdateWizard()
        self.pending_message_ids: list[tuple[int, int]] = []

    async def set_pending_message_id(self, pending_id: int, message_id: int) -> None:
        self.pending_message_ids.append((pending_id, message_id))
        await self.cards.set_pending_message_id(pending_id, message_id)


def update_with_message(
    message: FakeMessage,
    *,
    user_id: int = 2,
    chat_id: int = 2,
    chat_type: str = "private",
) -> object:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="user", full_name="User"),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title="Chat"),
        effective_message=message,
    )


def context_with_services(services: FakeServices) -> object:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"services": services}),
        bot=FakeBot(),
    )


def update_intent(*operations: PatchOperation) -> IntentResult:
    return IntentResult(
        intent="propose_update",
        confidence=0.95,
        answer=None,
        patches=list(operations),
        needs_clarification=False,
        clarification_question=None,
    )


def question_intent(answer: str = "Ответ по карточке.") -> IntentResult:
    return IntentResult(
        intent="question",
        confidence=0.95,
        answer=answer,
        patches=[],
        needs_clarification=False,
        clarification_question=None,
    )


async def access_for(
    database: object,
    app_config: object,
    *,
    role: str,
    chat_id: int = 2,
    chat_type: str = "private",
) -> tuple[AccessService, AccessContext]:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await service.observe(
        user_id=2,
        username="user",
        full_name="User",
        chat_id=2,
        chat_type="private",
        chat_title=None,
    )
    await service.decide_user(1, 2, status="approved", role=role)
    access = await service.observe(
        user_id=2,
        username="user",
        full_name="User",
        chat_id=chat_id,
        chat_type=chat_type,
        chat_title="Chat",
    )
    if chat_type in {"group", "supergroup"}:
        await service.decide_chat(1, chat_id, status="approved")
        access = await service.observe(
            user_id=2,
            username="user",
            full_name="User",
            chat_id=chat_id,
            chat_type=chat_type,
            chat_title="Chat",
        )
    return service, access


async def test_editor_natural_text_creates_pending_and_confirm_applies(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, access = await access_for(database, app_config, role="editor")
    await card_service.get_or_create_current()
    llm = FakeLlm(
        update_intent(
            PatchOperation(
                op="set_field",
                block_id="speaker",
                field_id="name",
                value="Иван Иванов",
                human_label="Спикер — Имя",
            )
        )
    )
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    message = FakeMessage("спикер Иван Иванов")
    context = context_with_services(services)

    await messages.process_natural_text(
        update_with_message(message),
        context,
        access,
        message.text or "",
    )

    assert context.bot.chat_actions == [(2, ChatAction.TYPING)]
    assert "Предлагаемые изменения" in message.replies[0][0]
    assert "Спикер — Имя: Иван Иванов" in message.replies[0][0]
    assert llm.calls[0]["text"] == "спикер Иван Иванов"
    pending = (await card_service.pending_for_user(2))[0]
    assert services.pending_message_ids == [(pending.id, 101)]

    before = await card_service.get_or_create_current()
    assert card_service.card_data(before)["blocks"]["speaker"]["fields"] == {}

    await card_service.resolve_pending(pending.id, 2, approve=True)
    after = await card_service.get_or_create_current()
    assert card_service.card_data(after)["blocks"]["speaker"]["fields"]["name"] == "Иван Иванов"


async def test_viewer_natural_update_is_read_only(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, access = await access_for(database, app_config, role="viewer")
    await card_service.get_or_create_current()
    llm = FakeLlm(
        update_intent(
            PatchOperation(op="set_field", block_id="speaker", field_id="name", value="Иван")
        )
    )
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    message = FakeMessage("спикер Иван")
    context = context_with_services(services)

    await messages.process_natural_text(
        update_with_message(message),
        context,
        access,
        message.text or "",
    )

    assert context.bot.chat_actions == [(2, ChatAction.TYPING)]
    assert message.replies == [("У тебя доступ read-only; изменить карточку нельзя.", {})]
    assert await card_service.pending_for_user(2) == []


async def test_invalid_llm_patch_gets_user_facing_reply(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, access = await access_for(database, app_config, role="editor")
    await card_service.get_or_create_current()
    llm = FakeLlm(
        update_intent(
            PatchOperation(
                op="set_field",
                block_id="announcements",
                field_id="approved",
                value="Да",
            )
        )
    )
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    message = FakeMessage("объявлению согласовано да")

    await messages.process_natural_text(
        update_with_message(message),
        context_with_services(services),
        access,
        message.text or "",
    )

    assert "не смог безопасно подготовить preview" in message.replies[0][0]
    assert "нужно выбрать конкретный экземпляр" in message.replies[0][0]


async def test_group_text_without_tag_is_ignored(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, _ = await access_for(
        database,
        app_config,
        role="editor",
        chat_id=-100,
        chat_type="supergroup",
    )
    llm = FakeLlm(update_intent())
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    message = FakeMessage("спикер Иван")

    await messages.text_message(
        update_with_message(message, chat_id=-100, chat_type="supergroup"),
        context_with_services(services),
    )

    assert message.replies == []
    assert llm.calls == []


async def test_group_tagged_question_calls_llm_with_stripped_text(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, _ = await access_for(
        database,
        app_config,
        role="viewer",
        chat_id=-100,
        chat_type="supergroup",
    )
    llm = FakeLlm(question_intent("Главный спикер: Иван."))
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    message = FakeMessage("@Planner40Bot, кто главный спикер?")
    context = context_with_services(services)

    await messages.text_message(
        update_with_message(message, user_id=42, chat_id=-100, chat_type="supergroup"),
        context,
    )

    assert context.bot.chat_actions == [(-100, ChatAction.TYPING)]
    assert llm.calls[0]["text"] == "кто главный спикер?"
    assert llm.calls[0]["role"] == "viewer"
    assert message.replies == [("Главный спикер: Иван.", {})]
    assert [user.telegram_user_id for user in await access_service.users()] == [1, 2]


async def test_group_mention_matching_is_case_insensitive_and_exact(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, _ = await access_for(
        database,
        app_config,
        role="viewer",
        chat_id=-100,
        chat_type="supergroup",
    )
    llm = FakeLlm(question_intent())
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    wrong = FakeMessage("@Planner40BotX кто главный спикер?")
    right = FakeMessage("@planner40bot: кто главный спикер?")

    await messages.text_message(
        update_with_message(wrong, user_id=42, chat_id=-100, chat_type="supergroup"),
        context_with_services(services),
    )
    await messages.text_message(
        update_with_message(right, user_id=42, chat_id=-100, chat_type="supergroup"),
        context_with_services(services),
    )

    assert wrong.replies == []
    assert len(llm.calls) == 1
    assert llm.calls[0]["text"] == "кто главный спикер?"


async def test_group_empty_tag_does_not_call_llm(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, _ = await access_for(
        database,
        app_config,
        role="viewer",
        chat_id=-100,
        chat_type="supergroup",
    )
    llm = FakeLlm(question_intent())
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    message = FakeMessage("@Planner40Bot")

    await messages.text_message(
        update_with_message(message, user_id=42, chat_id=-100, chat_type="supergroup"),
        context_with_services(services),
    )

    assert message.replies == [("Напиши вопрос после @BOTNAME.", {})]
    assert llm.calls == []


async def test_pending_group_chat_does_not_call_llm_and_notifies_admin(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service = AccessService(database, app_config)
    await access_service.ensure_root_admin()
    llm = FakeLlm(question_intent())
    services = FakeServices(
        config=app_config,
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    message = FakeMessage("@Planner40Bot кто главный спикер?")
    context = context_with_services(services)

    await messages.text_message(
        update_with_message(message, user_id=42, chat_id=-100, chat_type="supergroup"),
        context,
    )

    assert context.bot.chat_actions == []
    assert "Этот чат пока не одобрен" in message.replies[0][0]
    assert llm.calls == []
    assert context.bot.messages[0][0] == app_config.telegram.admin_user_id
    assert "Новая заявка чата" in context.bot.messages[0][1]


async def test_blocked_user_in_group_does_not_call_llm(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, _ = await access_for(
        database,
        app_config,
        role="viewer",
        chat_id=-100,
        chat_type="supergroup",
    )
    await access_service.observe(
        user_id=42,
        username="blocked",
        full_name="Blocked",
        chat_id=42,
        chat_type="private",
        chat_title=None,
    )
    await access_service.decide_user(1, 42, status="blocked")
    llm = FakeLlm(question_intent())
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    message = FakeMessage("@Planner40Bot кто главный спикер?")
    context = context_with_services(services)

    await messages.text_message(
        update_with_message(message, user_id=42, chat_id=-100, chat_type="supergroup"),
        context,
    )

    assert context.bot.chat_actions == []
    assert message.replies == [("Доступ заблокирован. Обратитесь к администратору.", {})]
    assert llm.calls == []


async def test_group_tagged_update_is_read_only(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, _ = await access_for(
        database,
        app_config,
        role="editor",
        chat_id=-100,
        chat_type="supergroup",
    )
    llm = FakeLlm(
        update_intent(
            PatchOperation(op="set_field", block_id="speaker", field_id="name", value="Иван")
        )
    )
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
    )
    message = FakeMessage("@Planner40Bot поставь спикера Иван")

    await messages.text_message(
        update_with_message(message, chat_id=-100, chat_type="supergroup"),
        context_with_services(services),
    )

    assert message.replies == [("У тебя доступ read-only; изменить карточку нельзя.", {})]
    assert await card_service.pending_for_user(2) == []


async def test_voice_message_reuses_natural_text_flow(
    database,
    app_config,
    card_service,
    loaded_schema,
) -> None:
    access_service, _ = await access_for(database, app_config, role="editor")
    await card_service.get_or_create_current()
    llm = FakeLlm(
        update_intent(
            PatchOperation(
                op="set_field",
                block_id="speaker",
                field_id="name",
                value="Иван Иванов",
            )
        )
    )
    services = FakeServices(
        access=access_service,
        cards=card_service,
        loaded_schema=loaded_schema,
        llm=llm,
        voice=FakeVoice("спикер Иван Иванов"),
    )
    message = FakeMessage(voice=SimpleNamespace(file_id="voice-1", file_size=100))
    context = context_with_services(services)

    await messages.voice_message(
        update_with_message(message),
        context,
    )

    assert context.bot.chat_actions == [(2, ChatAction.TYPING), (2, ChatAction.TYPING)]
    assert "Я распознал" in message.replies[0][0]
    assert llm.calls[0]["text"] == "спикер Иван Иванов"
    assert "Предлагаемые изменения" in message.replies[1][0]
