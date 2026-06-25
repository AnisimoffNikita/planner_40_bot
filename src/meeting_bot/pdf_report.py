from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from meeting_bot.card_service import StatusBlock
from meeting_bot.config import PdfConfig
from meeting_bot.models import MeetingCard
from meeting_bot.readiness import DeadlineState, FieldEvaluation, ValueState
from meeting_bot.schema import MeetingSchema

KNOWN_FONTS = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
)


class PdfReportError(RuntimeError):
    pass


class PdfReportBuilder:
    def __init__(self, config: PdfConfig) -> None:
        self.config = config
        self.font_path = self._find_font()
        self.font_name = "MeetingBotDejaVu"
        if self.font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(self.font_name, str(self.font_path)))

    def _find_font(self) -> Path:
        candidates = ([self.config.font_path] if self.config.font_path else []) + list(KNOWN_FONTS)
        for candidate in candidates:
            if candidate is not None and candidate.is_file():
                return candidate
        raise PdfReportError("Не найден TTF-шрифт с поддержкой кириллицы. Укажите pdf.font_path.")

    def build(
        self,
        card: MeetingCard,
        schema: MeetingSchema,
        status_blocks: list[StatusBlock],
        *,
        schema_fallback: bool = False,
    ) -> Path:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.config.output_dir / f"meeting_status_{card.week_start_date}.pdf"
        fd, temporary_name = tempfile.mkstemp(
            suffix=".pdf", prefix=".meeting-status-", dir=self.config.output_dir
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            self._render(temporary_path, card, schema, status_blocks, schema_fallback)
            os.replace(temporary_path, final_path)
            return final_path
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def _render(
        self,
        path: Path,
        card: MeetingCard,
        schema: MeetingSchema,
        status_blocks: list[StatusBlock],
        schema_fallback: bool,
    ) -> None:
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "MeetingTitle",
            parent=styles["Title"],
            fontName=self.font_name,
            fontSize=17,
            leading=21,
            alignment=TA_CENTER,
            spaceAfter=8,
        )
        heading_style = ParagraphStyle(
            "BlockHeading",
            parent=styles["Heading2"],
            fontName=self.font_name,
            fontSize=13,
            leading=16,
            spaceBefore=8,
            spaceAfter=5,
        )
        body_style = ParagraphStyle(
            "Body",
            parent=styles["BodyText"],
            fontName=self.font_name,
            fontSize=9,
            leading=12,
        )
        warning_style = ParagraphStyle(
            "Warning",
            parent=body_style,
            textColor=colors.HexColor("#8A5A00"),
            backColor=colors.HexColor("#FFF4CC"),
            borderPadding=5,
        )
        document = SimpleDocTemplate(
            str(path),
            pagesize=A4,
            leftMargin=14 * mm,
            rightMargin=14 * mm,
            topMargin=14 * mm,
            bottomMargin=14 * mm,
            title=f"{schema.title} — {card.week_start_date}",
            author="meeting_bot",
        )
        story: list[object] = [
            Paragraph(schema.title, title_style),
            Paragraph(
                f"Неделя с {card.week_start_date} · схема {card.schema_version} · "
                f"сформировано {datetime.now().astimezone():%d.%m.%Y %H:%M}",
                body_style,
            ),
            Spacer(1, 6 * mm),
        ]
        if schema_fallback:
            story.extend(
                [
                    Paragraph(
                        "Сохраненная схема этой недели недоступна. Отчет построен "
                        "best-effort по текущей схеме.",
                        warning_style,
                    ),
                    Spacer(1, 4 * mm),
                ]
            )
        if not status_blocks:
            story.append(Paragraph("В карточке пока нет отображаемых блоков.", body_style))
        for index, block in enumerate(status_blocks):
            if index and index % 5 == 0:
                story.append(PageBreak())
            heading = block.title
            if block.entry_title:
                heading = f"{heading}: {block.entry_title}"
            story.append(Paragraph(heading, heading_style))
            rows: list[list[object]] = [
                [
                    Paragraph("<b>Название</b>", body_style),
                    Paragraph("<b>Статус</b>", body_style),
                ]
            ]
            backgrounds: list[tuple[int, colors.Color]] = []
            for row_number, field in enumerate(block.fields, start=1):
                rows.append(
                    [
                        Paragraph(field.field_label, body_style),
                        Paragraph(self._status_text(field.evaluation), body_style),
                    ]
                )
                backgrounds.append((row_number, self._row_color(field.evaluation)))
            table = Table(rows, colWidths=[78 * mm, 96 * mm], repeatRows=1, hAlign="LEFT")
            commands: list[tuple[object, ...]] = [
                ("FONTNAME", (0, 0), (-1, -1), self.font_name),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EEF5")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#BBC3CD")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
            commands.extend(
                ("BACKGROUND", (0, row), (-1, row), color) for row, color in backgrounds
            )
            table.setStyle(TableStyle(commands))
            story.extend([table, Spacer(1, 4 * mm)])
        document.build(story)

    @staticmethod
    def _status_text(evaluation: FieldEvaluation) -> str:
        value_state = evaluation.value_state
        value = evaluation.value
        if value_state == ValueState.OPTIONAL:
            return f"Не требуется: {value}" if value else "Не требуется"
        if value_state == ValueState.READY:
            return f"Готово: {value}" if value else "Готово"
        if value_state == ValueState.MISSING:
            base = "Не заполнено"
        else:
            base = value or "В процессе"
        if evaluation.deadline_state == DeadlineState.OVERDUE:
            return f"{base} · дедлайн прошел"
        if evaluation.deadline_state == DeadlineState.DUE_TODAY:
            return f"{base} · дедлайн сегодня"
        return base

    @staticmethod
    def _row_color(evaluation: FieldEvaluation) -> colors.Color:
        if evaluation.value_state == ValueState.READY:
            return colors.HexColor("#E4F4E7")
        if evaluation.deadline_state == DeadlineState.OVERDUE:
            return colors.HexColor("#F8E0E0")
        if evaluation.deadline_state == DeadlineState.DUE_TODAY:
            return colors.HexColor("#FFF3BF")
        return colors.HexColor("#FAFAFA")
