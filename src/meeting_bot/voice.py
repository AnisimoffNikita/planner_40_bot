from __future__ import annotations

import tempfile
from pathlib import Path

from telegram import File, Voice

from meeting_bot.llm_client import LlmClient


class VoiceService:
    def __init__(self, llm: LlmClient, max_bytes: int) -> None:
        self.llm = llm
        self.max_bytes = max_bytes

    async def transcribe_telegram_voice(self, voice: Voice, telegram_file: File) -> str:
        if voice.file_size is not None and voice.file_size > self.max_bytes:
            raise ValueError("Голосовое сообщение слишком большое.")
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp:
                temp_path = Path(temp.name)
            await telegram_file.download_to_drive(custom_path=temp_path)
            if temp_path.stat().st_size > self.max_bytes:
                raise ValueError("Голосовое сообщение слишком большое.")
            return await self.llm.transcribe(str(temp_path))
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
