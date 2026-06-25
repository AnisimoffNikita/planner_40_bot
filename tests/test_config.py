from pathlib import Path

from meeting_bot.config import load_app_config


def test_env_expansion_and_relative_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    path = tmp_path / "app.yaml"
    path.write_text(
        """
telegram:
  token: "${BOT_TOKEN}"
  admin_user_id: 42
app:
  database_path: "./data/db.sqlite3"
llm:
  enabled: false
pdf:
  output_dir: "./reports"
  font_path: null
""",
        encoding="utf-8",
    )
    config = load_app_config(path)
    assert config.telegram.token.get_secret_value() == "123:abc"
    assert config.app.database_path == tmp_path / "data/db.sqlite3"
    assert config.pdf.output_dir == tmp_path / "reports"
