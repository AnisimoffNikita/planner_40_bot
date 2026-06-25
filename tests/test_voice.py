from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_bot.voice import VoiceService


class FakeLlm:
    def __init__(self) -> None:
        self.path: Path | None = None

    async def transcribe(self, file_path: str) -> str:
        self.path = Path(file_path)
        assert self.path.exists()
        return "распознано"


class FakeTelegramFile:
    async def download_to_drive(self, custom_path: Path) -> None:
        custom_path.write_bytes(b"ogg-data")


async def test_voice_temp_file_is_removed() -> None:
    llm = FakeLlm()
    service = VoiceService(llm, max_bytes=100)
    text = await service.transcribe_telegram_voice(SimpleNamespace(file_size=8), FakeTelegramFile())
    assert text == "распознано"
    assert llm.path is not None
    assert not llm.path.exists()


async def test_voice_size_limit_checked_before_download() -> None:
    service = VoiceService(FakeLlm(), max_bytes=5)
    with pytest.raises(ValueError, match="слишком большое"):
        await service.transcribe_telegram_voice(SimpleNamespace(file_size=10), FakeTelegramFile())
