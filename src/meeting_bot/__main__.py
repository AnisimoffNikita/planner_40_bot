from __future__ import annotations

import argparse
import logging
from pathlib import Path

from telegram import Update

from meeting_bot.bot import build_application
from meeting_bot.config import load_app_config
from meeting_bot.schema import load_meeting_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Weekly meeting preparation Telegram bot")
    parser.add_argument("--app-config", required=True, type=Path)
    parser.add_argument("--meeting-schema", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_app_config(args.app_config)
    logging.basicConfig(
        level=getattr(logging, config.app.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    loaded_schema = load_meeting_schema(args.meeting_schema)
    logging.getLogger(__name__).info(
        "Starting meeting bot with schema version=%s hash=%s",
        loaded_schema.schema.version,
        loaded_schema.schema_hash[:12],
    )
    application = build_application(config, loaded_schema)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
