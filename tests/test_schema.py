from pathlib import Path

import pytest

from meeting_bot.schema import load_meeting_schema


def test_real_schema_parses_and_hash_is_stable() -> None:
    first = load_meeting_schema(Path("service_schema.yaml"))
    second = load_meeting_schema(Path("service_schema.yaml"))

    assert first.schema.version == "4.0"
    assert len(first.schema.blocks) == 12
    assert first.schema_hash == second.schema_hash
    assert len(first.schema_hash) == 64


def test_duplicate_block_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
version: "1"
title: "Bad"
blocks:
  - id: same
    title: A
    multiple: false
    fields:
      x: {label: X, allowed_values: [Да], ready_if: [Да], deadline: null}
  - id: same
    title: B
    multiple: false
    fields:
      y: {label: Y, allowed_values: [Да], ready_if: [Да], deadline: null}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate block"):
        load_meeting_schema(path)


def test_duplicate_yaml_field_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-key.yaml"
    path.write_text(
        """
version: "1"
title: "Bad"
blocks:
  - id: block
    title: Block
    multiple: false
    fields:
      value: {label: First, allowed_values: [Да], ready_if: [Да], deadline: null}
      value: {label: Second, allowed_values: [Да], ready_if: [Да], deadline: null}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate key"):
        load_meeting_schema(path)
