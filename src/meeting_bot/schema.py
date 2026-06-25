from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
SPECIAL_READY_RULES = {
    "Не пусто",
    "Любое значение",
    "Любое значение или его отсутствие",
}


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class DeadlineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: int = Field(ge=1, le=7)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str = Field(min_length=1)
    allowed_values: list[str] = Field(default_factory=list)
    ready_if: list[str] = Field(min_length=1)
    deadline: DeadlineSpec | None

    @field_validator("allowed_values", "ready_if")
    @classmethod
    def normalize_values(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("Values cannot be empty")
        return normalized


class BlockSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    title: str = Field(min_length=1)
    multiple: bool
    fields: dict[str, FieldSpec] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not ID_PATTERN.fullmatch(value):
            raise ValueError("Block id must contain only letters, numbers, '_' or '-'")
        return value

    @field_validator("fields")
    @classmethod
    def validate_field_ids(cls, value: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
        invalid = [field_id for field_id in value if not ID_PATTERN.fullmatch(field_id)]
        if invalid:
            raise ValueError(f"Invalid field ids: {', '.join(invalid)}")
        return value


class MeetingSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str
    title: str = Field(min_length=1)
    blocks: list[BlockSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_blocks(self) -> MeetingSchema:
        ids = [block.id for block in self.blocks]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"Duplicate block ids: {', '.join(duplicates)}")
        return self

    @property
    def block_map(self) -> dict[str, BlockSpec]:
        return {block.id: block for block in self.blocks}

    def compact_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "title": self.title,
            "blocks": [
                {
                    "id": block.id,
                    "title": block.title,
                    "multiple": block.multiple,
                    "fields": {
                        field_id: {
                            "label": field.label,
                            "allowed_values": field.allowed_values,
                            "ready_if": field.ready_if,
                            "deadline": (
                                field.deadline.model_dump() if field.deadline is not None else None
                            ),
                        }
                        for field_id, field in block.fields.items()
                    },
                }
                for block in self.blocks
            ],
        }


@dataclass(frozen=True)
class LoadedSchema:
    schema: MeetingSchema
    schema_hash: str
    canonical_json: str
    source_path: Path
    warnings: list[str] = dataclass_field(default_factory=list)


def _semantic_warnings(schema: MeetingSchema) -> list[str]:
    warnings: list[str] = []
    for block in schema.blocks:
        for field_id, field in block.fields.items():
            allowed_folded = {item.casefold() for item in field.allowed_values}
            for ready_value in field.ready_if:
                is_placeholder = ready_value.startswith("<") and ready_value.endswith(">")
                if (
                    ready_value not in SPECIAL_READY_RULES
                    and not is_placeholder
                    and ready_value.casefold() not in allowed_folded
                ):
                    warnings.append(
                        f"{block.id}.{field_id}: ready_if value {ready_value!r} "
                        "is not listed in allowed_values"
                    )
    return warnings


def load_meeting_schema(path: str | Path) -> LoadedSchema:
    source_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.load(source_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load meeting schema {source_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Meeting schema must be a YAML object")
    schema = MeetingSchema.model_validate(raw)
    canonical_json = json.dumps(
        schema.compact_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    schema_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    warnings = _semantic_warnings(schema)
    for warning in warnings:
        logger.warning("Schema warning: %s", warning)
    return LoadedSchema(
        schema=schema,
        schema_hash=schema_hash,
        canonical_json=canonical_json,
        source_path=source_path,
        warnings=warnings,
    )
