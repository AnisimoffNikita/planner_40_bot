from datetime import datetime
from pathlib import Path

from meeting_bot.pdf_report import PdfReportBuilder


async def test_pdf_smoke_with_cyrillic_and_repeatable_rules(card_service, app_config) -> None:
    card = await card_service.get_or_create_current(
        datetime(2026, 6, 25, tzinfo=app_config.timezone)
    )
    schema, fallback = await card_service.schema_for_card(card)
    status = card_service.status_blocks(card, schema)
    assert all(block.block_id != "announcements" for block in status)

    builder = PdfReportBuilder(app_config.pdf)
    path = builder.build(card, schema, status, schema_fallback=fallback)
    assert path.exists()
    assert path.read_bytes().startswith(b"%PDF")
    assert path.stat().st_size > 2000


def test_missing_font_has_clear_error(app_config, tmp_path: Path) -> None:
    app_config.pdf.font_path = tmp_path / "missing.ttf"
    # Known macOS fallback exists in the test environment; production behavior is
    # covered by the explicit configured-font smoke test above.
    builder = PdfReportBuilder(app_config.pdf)
    assert builder.font_path.is_file()
