from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from meeting_bot.access import AccessService
from meeting_bot.card_service import CardService, DomainError
from meeting_bot.command_catalog import sync_all_command_menus
from meeting_bot.config import AppConfig
from meeting_bot.handlers import admin, callbacks, commands, messages, update_wizard
from meeting_bot.intent_parser import ClarificationService
from meeting_bot.llm_client import LlmClient, LlmUnavailable
from meeting_bot.notifications import NotificationService
from meeting_bot.pdf_report import PdfReportBuilder
from meeting_bot.schema import LoadedSchema
from meeting_bot.storage import Database
from meeting_bot.update_wizard import UpdateWizardService
from meeting_bot.voice import VoiceService

logger = logging.getLogger(__name__)


@dataclass
class BotServices:
    config: AppConfig
    loaded_schema: LoadedSchema
    database: Database
    cards: CardService
    access: AccessService
    llm: LlmClient
    voice: VoiceService
    pdf: PdfReportBuilder
    notifications: NotificationService
    clarifications: ClarificationService
    update_wizard: UpdateWizardService

    async def set_pending_message_id(self, pending_id: int, message_id: int) -> None:
        await self.cards.set_pending_message_id(pending_id, message_id)


def create_services(config: AppConfig, loaded_schema: LoadedSchema) -> BotServices:
    database = Database(config.app.database_path)
    cards = CardService(database, config, loaded_schema)
    access = AccessService(database, config)
    llm = LlmClient(config.llm)
    voice = VoiceService(llm, config.llm.max_voice_bytes)
    pdf = PdfReportBuilder(config.pdf)
    notifications = NotificationService(database, config, cards, access)
    update_service = UpdateWizardService(database, cards)
    return BotServices(
        config=config,
        loaded_schema=loaded_schema,
        database=database,
        cards=cards,
        access=access,
        llm=llm,
        voice=voice,
        pdf=pdf,
        notifications=notifications,
        clarifications=ClarificationService(database),
        update_wizard=update_service,
    )


async def _post_init(application: Application) -> None:
    services: BotServices = application.bot_data["services"]
    await services.database.initialize()
    await services.access.ensure_root_admin()
    await services.cards.get_or_create_current()
    await sync_all_command_menus(application.bot, await services.access.users())
    if services.config.notifications.enabled and application.job_queue is not None:
        application.job_queue.run_repeating(
            _notification_job,
            interval=services.config.notifications.scheduler_interval_seconds,
            first=5,
            name="meeting-reminders",
        )


async def _post_shutdown(application: Application) -> None:
    services: BotServices = application.bot_data["services"]
    await services.database.close()


async def _notification_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    services: BotServices = context.application.bot_data["services"]
    try:
        await services.notifications.run(context.bot)
        await services.clarifications.cleanup()
        await services.update_wizard.cleanup()
    except Exception:
        logger.exception("Notification job failed")


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, (DomainError, PermissionError, ValueError, LlmUnavailable)):
        text = str(error)
        logger.info("User-facing error: %s", text)
    else:
        text = "Произошла внутренняя ошибка. Попробуйте еще раз позже."
        logger.exception("Unhandled Telegram update error", exc_info=error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(text)
        except Exception:
            logger.exception("Failed to send error response")


def build_application(config: AppConfig, loaded_schema: LoadedSchema) -> Application:
    services = create_services(config, loaded_schema)
    defaults = Defaults(parse_mode=config.app.default_parse_mode)
    application = (
        ApplicationBuilder()
        .token(config.telegram.token.get_secret_value())
        .defaults(defaults)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["services"] = services

    for name, handler in {
        "start": commands.start,
        "help": commands.help_command,
        "whoami": commands.whoami,
        "status": commands.status_command,
        "summary": commands.summary_command,
        "history": commands.history_command,
        "schema": commands.schema_command,
        "update": update_wizard.update_command,
        "pending": commands.pending_command,
        "cancel": commands.cancel_command,
        "users": admin.users_command,
        "approve": admin.approve_command,
        "reject": admin.reject_command,
        "block_user": admin.block_user_command,
        "unblock_user": admin.unblock_user_command,
        "block_chat": admin.block_chat_command,
        "unblock_chat": admin.unblock_chat_command,
        "audit": admin.audit_command,
    }.items():
        application.add_handler(CommandHandler(name, handler))
    application.add_handler(
        CallbackQueryHandler(callbacks.pending_callback, pattern=r"^p:[ac]:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(callbacks.user_approval_callback, pattern=r"^u:[ver]:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(commands.summary_callback, pattern=r"^s:(pdf|overdue|today)$")
    )
    application.add_handler(
        CallbackQueryHandler(
            update_wizard.wizard_callback,
            pattern=r"^uw:(opt:o\d+|act:(back|cancel|prev|next))$",
        )
    )
    application.add_handler(
        MessageHandler(filters.VOICE & ~filters.COMMAND, messages.voice_message)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messages.text_message))
    application.add_error_handler(_error_handler)
    return application
