from meeting_bot.access import AccessService


async def test_approval_and_group_read_only(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    access = await service.observe(
        user_id=2,
        username="editor",
        full_name="Editor",
        chat_id=2,
        chat_type="private",
        chat_title=None,
    )
    assert access.user.status == "pending"
    assert not access.can_use_llm

    await service.decide_user(1, 2, status="approved", role="editor")
    private = await service.observe(
        user_id=2,
        username="editor",
        full_name="Editor",
        chat_id=2,
        chat_type="private",
        chat_title=None,
    )
    group = await service.observe(
        user_id=2,
        username="editor",
        full_name="Editor",
        chat_id=-100,
        chat_type="supergroup",
        chat_title="Group",
    )
    assert private.can_edit
    assert group.chat.read_only
    assert group.chat.status == "pending"
    assert not group.can_use_llm
    assert not group.can_edit

    await service.decide_chat(1, -100, status="approved")
    approved_group = await service.observe(
        user_id=2,
        username="editor",
        full_name="Editor",
        chat_id=-100,
        chat_type="supergroup",
        chat_title="Group",
    )
    assert approved_group.can_use_llm
    assert not approved_group.can_edit


async def test_approved_group_allows_unregistered_actor_without_creating_user(
    database, app_config
) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await service.observe_group_chat(chat_id=-100, chat_type="supergroup", chat_title="Group")
    await service.decide_chat(1, -100, status="approved")

    access = await service.observe(
        user_id=42,
        username="guest",
        full_name="Guest",
        chat_id=-100,
        chat_type="supergroup",
        chat_title="Group",
    )

    assert access.can_use_llm
    assert access.user.telegram_user_id == 42
    assert [user.telegram_user_id for user in await service.users()] == [1]


async def test_blocked_user_cannot_use_approved_group_llm(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await service.observe(
        user_id=3,
        username=None,
        full_name="Blocked",
        chat_id=3,
        chat_type="private",
        chat_title=None,
    )
    await service.decide_user(1, 3, status="blocked")
    await service.observe_group_chat(chat_id=-100, chat_type="supergroup", chat_title="Group")
    await service.decide_chat(1, -100, status="approved")

    access = await service.observe(
        user_id=3,
        username=None,
        full_name="Blocked",
        chat_id=-100,
        chat_type="supergroup",
        chat_title="Group",
    )

    assert access.blocked
    assert not access.can_use_llm


async def test_rejected_user_can_use_approved_group_llm(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await service.observe(
        user_id=4,
        username=None,
        full_name="Rejected",
        chat_id=4,
        chat_type="private",
        chat_title=None,
    )
    await service.decide_user(1, 4, status="rejected")
    await service.observe_group_chat(chat_id=-100, chat_type="supergroup", chat_title="Group")
    await service.decide_chat(1, -100, status="approved")

    access = await service.observe(
        user_id=4,
        username=None,
        full_name="Rejected",
        chat_id=-100,
        chat_type="supergroup",
        chat_title="Group",
    )

    assert not access.blocked
    assert access.can_use_llm


async def test_blocked_user_cannot_use_llm(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    await service.observe(
        user_id=3,
        username=None,
        full_name="Blocked",
        chat_id=3,
        chat_type="private",
        chat_title=None,
    )
    await service.decide_user(1, 3, status="blocked")
    access = await service.observe(
        user_id=3,
        username=None,
        full_name="Blocked",
        chat_id=3,
        chat_type="private",
        chat_title=None,
    )
    assert access.blocked
    assert not access.can_use_llm


async def test_root_admin_role_cannot_be_changed(database, app_config) -> None:
    service = AccessService(database, app_config)
    await service.ensure_root_admin()
    try:
        await service.decide_user(1, 1, status="approved", role="viewer")
    except ValueError as exc:
        assert "root-admin" in str(exc)
    else:
        raise AssertionError("Root admin role change must fail")
