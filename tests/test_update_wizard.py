import json
from pathlib import Path

from meeting_bot.access import AccessService
from meeting_bot.card_service import CardService
from meeting_bot.domain import PatchOperation
from meeting_bot.schema import load_meeting_schema
from meeting_bot.update_wizard import UpdateWizardService, WizardRender


async def approve_editor(database, app_config, user_id: int = 2) -> None:
    access = AccessService(database, app_config)
    await access.ensure_root_admin()
    await access.observe(
        user_id=user_id,
        username="editor",
        full_name="Editor",
        chat_id=user_id,
        chat_type="private",
        chat_title=None,
    )
    await access.decide_user(1, user_id, status="approved", role="editor")


def button_labels(render: WizardRender) -> list[str]:
    return [button.label for row in render.rows for button in row]


async def test_wizard_uses_russian_labels_not_schema_ids(
    database, app_config, card_service
) -> None:
    await card_service.get_or_create_current()
    service = UpdateWizardService(database, card_service)

    render = await service.start(2, 2)
    labels = button_labels(render)
    assert "Спикер" in labels
    assert "Объявления" in labels
    assert "speaker" not in labels
    assert "announcements" not in labels

    outcome = await service.handle_callback(2, 2, "uw:opt:o0")
    assert outcome.render is not None
    outcome = await service.handle_callback(2, 2, "uw:opt:o0")
    assert outcome.render is not None
    labels = button_labels(outcome.render)
    assert "Имя" in labels
    assert "Слайды" in labels
    assert "name" not in labels
    assert "slides" not in labels


async def test_wizard_paginates_options_by_six_2x3(tmp_path: Path, database, app_config) -> None:
    schema_path = tmp_path / "many_blocks.yaml"
    blocks_yaml = "\n".join(
        f"""
  - id: block_{index}
    title: "Блок {index}"
    multiple: false
    fields:
      name:
        label: "Имя"
        allowed_values: ["Строка"]
        ready_if: ["Не пусто"]
        deadline: null
""".rstrip()
        for index in range(1, 8)
    )
    schema_path.write_text(
        f"""
version: "1.0"
title: "Много блоков"
blocks:
{blocks_yaml}
""",
        encoding="utf-8",
    )
    cards = CardService(database, app_config, load_meeting_schema(schema_path))
    await cards.get_or_create_current()
    service = UpdateWizardService(database, cards)

    render = await service.start(2, 2)
    assert [[button.label for button in row] for row in render.rows[:3]] == [
        ["Блок 1", "Блок 2"],
        ["Блок 3", "Блок 4"],
        ["Блок 5", "Блок 6"],
    ]
    assert "Блок 7" not in button_labels(render)
    assert ["→"] in [[button.label for button in row] for row in render.rows]

    outcome = await service.handle_callback(2, 2, "uw:act:next")
    assert outcome.render is not None
    labels = button_labels(outcome.render)
    assert "Блок 7" in labels
    assert "←" in labels


async def test_singleton_manual_input_creates_pending_without_changing_card(
    database, app_config, card_service
) -> None:
    await approve_editor(database, app_config)
    card = await card_service.get_or_create_current()
    service = UpdateWizardService(database, card_service)

    await service.start(2, 2)
    await service.handle_callback(2, 2, "uw:opt:o0")
    await service.handle_callback(2, 2, "uw:opt:o0")
    await service.handle_callback(2, 2, "uw:opt:o0")
    outcome = await service.handle_callback(2, 2, "uw:opt:o0")
    assert outcome.render is not None
    assert outcome.render.waiting_for_text

    outcome = await service.handle_text(2, 2, "Иван Иванов")
    assert outcome is not None
    assert outcome.pending is not None
    patch = json.loads(outcome.pending.patch_json)[0]
    assert patch["op"] == "set_field"
    assert patch["block_id"] == "speaker"
    assert patch["field_id"] == "name"
    assert patch["value"] == "Иван Иванов"
    assert card_service.card_data(card)["blocks"]["speaker"]["fields"] == {}


async def test_singleton_clear_creates_clear_block_pending(
    database, app_config, card_service
) -> None:
    await approve_editor(database, app_config)
    await card_service.get_or_create_current()
    set_pending = await card_service.create_pending(
        user_id=2,
        chat_id=2,
        operations=[
            PatchOperation(op="set_field", block_id="speaker", field_id="name", value="Иван")
        ],
    )
    await card_service.resolve_pending(set_pending.id, 2, approve=True)
    service = UpdateWizardService(database, card_service)

    await service.start(2, 2)
    await service.handle_callback(2, 2, "uw:opt:o0")
    outcome = await service.handle_callback(2, 2, "uw:opt:o1")
    assert outcome.pending is not None
    patch = json.loads(outcome.pending.patch_json)[0]
    assert patch["op"] == "clear_block"

    await card_service.resolve_pending(outcome.pending.id, 2, approve=True)
    card = await card_service.get_or_create_current()
    assert card_service.card_data(card)["blocks"]["speaker"]["fields"] == {}


async def test_multiple_flow_uses_entry_title_for_edit_add_and_delete(
    database, app_config, card_service
) -> None:
    await approve_editor(database, app_config)
    await card_service.get_or_create_current()
    added = await card_service.create_pending(
        user_id=2,
        chat_id=2,
        operations=[PatchOperation(op="add_entry", block_id="announcements", value="Лагерь")],
    )
    await card_service.resolve_pending(added.id, 2, approve=True)
    entry_id = json.loads(added.patch_json)[0]["entry_id"]
    service = UpdateWizardService(database, card_service)

    await service.start(2, 2)
    outcome = await service.handle_callback(2, 2, "uw:opt:o1")
    assert outcome.render is not None
    labels = button_labels(outcome.render)
    assert "Добавить новый" in labels
    assert "Лагерь" in labels
    assert entry_id not in labels

    outcome = await service.handle_callback(2, 2, "uw:opt:o1")
    assert outcome.render is not None
    await service.handle_callback(2, 2, "uw:opt:o0")
    await service.handle_callback(2, 2, "uw:opt:o1")
    outcome = await service.handle_callback(2, 2, "uw:opt:o2")
    assert outcome.pending is not None
    assert "Объявления / Лагерь — Согласовано: Не требуется" in outcome.pending.preview_text
    assert entry_id not in outcome.pending.preview_text

    await service.start(2, 2)
    await service.handle_callback(2, 2, "uw:opt:o1")
    await service.handle_callback(2, 2, "uw:opt:o0")
    outcome = await service.handle_text(2, 2, "Новый пункт")
    assert outcome is not None
    assert outcome.pending is not None
    assert json.loads(outcome.pending.patch_json)[0]["op"] == "add_entry"

    await service.start(2, 2)
    await service.handle_callback(2, 2, "uw:opt:o1")
    await service.handle_callback(2, 2, "uw:opt:o1")
    outcome = await service.handle_callback(2, 2, "uw:opt:o1")
    assert outcome.pending is not None
    assert f"[{entry_id}]" not in outcome.pending.preview_text
    assert "Удалить «Лагерь» из «Объявления»" in outcome.pending.preview_text


async def test_fixed_values_are_buttons_and_manual_hints_request_text(
    database, app_config, card_service
) -> None:
    await card_service.get_or_create_current()
    service = UpdateWizardService(database, card_service)

    await service.start(2, 2)
    await service.handle_callback(2, 2, "uw:opt:o0")
    await service.handle_callback(2, 2, "uw:opt:o0")
    outcome = await service.handle_callback(2, 2, "uw:opt:o1")
    assert outcome.render is not None
    labels = button_labels(outcome.render)
    assert "Да" in labels
    assert "В процессе" in labels
    assert "Ввести вручную" not in labels

    await service.start(2, 2)
    await service.handle_callback(2, 2, "uw:opt:o0")
    await service.handle_callback(2, 2, "uw:opt:o0")
    outcome = await service.handle_callback(2, 2, "uw:opt:o0")
    assert outcome.render is not None
    labels = button_labels(outcome.render)
    assert "Ввести вручную" in labels
    assert "Имя Фамилия" not in labels
