"""สัญญาที่ทุกปลายทางต้องทำตาม"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChatRequest:
    model: str
    system_prompt: str
    messages: list
    max_tokens: int | None = None
    schema: dict | None = None
    schema_name: str = "response"
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    refusal: str = ""
    finish_reason: str = "stop"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Upstream:
    name = "base"
    supports_native_schema = False

    async def complete(self, req: ChatRequest) -> Completion:
        raise NotImplementedError

    async def list_models(self) -> list:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
