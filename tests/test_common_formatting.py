from __future__ import annotations

from types import SimpleNamespace

from meeting_bot.handlers.common import format_status_fields


def status_field(
    *,
    block_title: str,
    field_label: str,
    entry_title: str | None = None,
    value: str | None = None,
) -> object:
    return SimpleNamespace(
        block_title=block_title,
        field_label=field_label,
        entry_title=entry_title,
        evaluation=SimpleNamespace(value=value),
    )


def test_format_status_fields_groups_labels_without_values() -> None:
    text = format_status_fields(
        [
            status_field(block_title="Спикер", field_label="Слайды", value="В процессе"),
            status_field(block_title="Спикер", field_label="Имя", value="Иван"),
            status_field(
                block_title="Объявления",
                entry_title="Лагерь",
                field_label="QR",
                value="Не готово",
            ),
        ],
        "Просрочено",
    )

    assert text == (
        "Просрочено\n"
        "\n"
        "Спикер\n"
        "- Слайды\n"
        "- Имя\n"
        "\n"
        "Объявления — Лагерь\n"
        "- QR"
    )
    assert "В процессе" not in text
    assert "Иван" not in text
    assert "Не готово" not in text
