"""ปลายทางปลอมสำหรับทดสอบ"""
from __future__ import annotations

import json
from .base import ChatRequest, Completion, Upstream


def _sample_for(schema: dict | None) -> dict:
    if not schema or schema.get("type") not in ("object", "OBJECT"):
        return {"ok": True}
    out: dict = {}
    props = schema.get("properties") or {}
    for key, spec in props.items():
        if key not in schema.get("required", list(props)):
            continue
        enum = spec.get("enum")
        raw = spec.get("type")
        types = raw if isinstance(raw, list) else [raw]
        primary = str(next((t for t in types if t and t != "null"), "string")).lower()
        if enum:
            out[key] = "hold" if "hold" in enum else enum[0]
        elif primary == "number":
            out[key] = 0.0 if key == "confidence" else 1.0
        elif primary == "integer":
            out[key] = 0
        elif primary == "boolean":
            out[key] = False
        elif primary == "array":
            out[key] = []
        elif primary == "object":
            out[key] = {}
        else:
            out[key] = "mock response"
    return out


class MockUpstream(Upstream):
    name = "mock"
    supports_native_schema = True

    def __init__(self, *, models: dict):
        self._models = list(models)

    async def complete(self, req: ChatRequest) -> Completion:
        body = json.dumps(_sample_for(req.schema), ensure_ascii=False)
        prompt_chars = len(req.system_prompt) + sum(len(str(m.get("content", ""))) for m in req.messages)
        return Completion(
            text=body,
            prompt_tokens=prompt_chars // 4,
            completion_tokens=len(body) // 4,
        )

    async def list_models(self) -> list:
        return self._models
