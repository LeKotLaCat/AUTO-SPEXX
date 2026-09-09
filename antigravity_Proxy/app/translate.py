"""OpenAI JSON-Schema <-> รูปแบบของปลายทาง"""
from __future__ import annotations

import json

_TYPE_MAP = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "object": "OBJECT",
    "array": "ARRAY",
}

_DROP_KEYS = frozenset({
    "title", "additionalProperties", "$schema", "$id", "strict",
    "definitions", "$defs", "default", "examples"
})


class SchemaTranslationError(ValueError):
    pass


def _split_nullable(node: dict) -> tuple[str, bool]:
    raw = node.get("type")
    nullable = bool(node.get("nullable", False))
    if isinstance(raw, list):
        non_null = [t for t in raw if t != "null"]
        if not nullable:
            nullable = len(non_null) < len(raw)
        if len(non_null) != 1:
            raise SchemaTranslationError(f"ปลายทางรองรับ type เดียวต่อฟิลด์เท่านั้น ได้มา {raw!r}")
        raw = non_null[0]
    if raw is None:
        raw = "object" if "properties" in node else "string"
    if not isinstance(raw, str):
        raise SchemaTranslationError(f"type ต้องเป็น string หรือ list ได้มา {raw!r}")
    mapped = _TYPE_MAP.get(raw.lower())
    if mapped is None:
        raise SchemaTranslationError(f"ไม่รู้จัก type {raw!r}")
    return mapped, nullable


def to_gemini_schema(schema: dict) -> dict:
    if not isinstance(schema, dict):
        raise SchemaTranslationError("schema ต้องเป็น object")
    gtype, nullable = _split_nullable(schema)
    out = {"type": gtype}
    if nullable:
        out["nullable"] = True
    for key in ("description", "enum", "format", "minimum", "maximum"):
        if key in schema:
            out[key] = schema[key]
    if gtype == "OBJECT":
        props = schema.get("properties") or {}
        if props:
            out["properties"] = {k: to_gemini_schema(v) for k, v in props.items()}
            required = [k for k in schema.get("required", []) if k in props]
            if required:
                out["required"] = required
            out["propertyOrdering"] = list(props.keys())
    elif gtype == "ARRAY":
        items = schema.get("items")
        if items:
            out["items"] = to_gemini_schema(items)
    unknown = set(schema) - _DROP_KEYS - {
        "type", "description", "enum", "format", "minimum", "maximum",
        "nullable", "properties", "required", "items"
    }
    if unknown:
        out.setdefault("_dropped", sorted(unknown))
        out.pop("_dropped")
    return out


def extract_response_format(payload: dict) -> tuple[dict | None, str]:
    rf = payload.get("response_format")
    if not isinstance(rf, dict):
        return None, ""
    kind = rf.get("type")
    if kind == "json_schema":
        js = rf.get("json_schema") or {}
        schema = js.get("schema")
        if not isinstance(schema, dict):
            raise SchemaTranslationError("response_format.json_schema.schema หายไป")
        return schema, str(js.get("name") or "response")
    elif kind == "json_object":
        return {}, "json_object"
    return None, ""


def schema_instruction(schema: dict, name: str) -> str:
    if not schema:
        return "\n\nRespond with a single valid JSON object and nothing else. No markdown, no code fences, no commentary."
    return (
        f"\n\nYou must reply with a single JSON object named `{name}` that validates against this JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
        + "\nOutput the raw JSON object only — no markdown fences, no explanation, no extra keys beyond those in the schema."
    )


def strip_json_fences(text: str) -> str:
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    s = s[3:]
    if s[:4].lower() == "json":
        s = s[4:]
    end = s.rfind("```")
    if end != -1:
        s = s[:end]
    return s.strip()
