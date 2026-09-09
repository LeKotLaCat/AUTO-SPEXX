"""ปลายทางที่พูด OpenAI อยู่แล้ว"""
from __future__ import annotations

import httpx

from ..errors import UpstreamError, classify_upstream_status, describe, transient
from .base import ChatRequest, Completion, Upstream


class OpenAICompatUpstream(Upstream):
    name = "openai_compat"
    supports_native_schema = True

    def __init__(self, *, base_url: str, api_key: str, timeout: int):
        self._base = (base_url or "http://localhost:11434/v1").rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )

    async def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def complete(self, req: ChatRequest) -> Completion:
        messages = []
        if req.system_prompt:
            messages.append({"role": "system", "content": req.system_prompt})
        messages.extend(req.messages)

        body: dict = {"model": req.model, "messages": messages}
        if req.max_tokens:
            body["max_tokens"] = req.max_tokens
        if req.schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.schema_name or "response", "schema": req.schema},
            }

        url = f"{self._base}/chat/completions"
        try:
            resp = await self._client.post(url, json=body, headers=await self._headers())
        except httpx.TimeoutException as exc:
            raise transient(f"upstream timeout: {describe(exc)}") from exc
        except httpx.HTTPError as exc:
            raise transient(f"cannot reach upstream, unavailable: {describe(exc)}") from exc

        if resp.status_code >= 400:
            raise classify_upstream_status(resp.status_code, resp.text)

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise UpstreamError("upstream returned no choices", kind="other")

        msg = choices[0].get("message") or {}
        if msg.get("refusal"):
            return Completion(text="", refusal=str(msg["refusal"]), finish_reason="content_filter")

        usage = data.get("usage") or {}
        return Completion(
            text=msg.get("content") or "",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=str(choices[0].get("finish_reason") or "stop"),
        )

    async def list_models(self) -> list:
        try:
            resp = await self._client.get(f"{self._base}/models", headers=await self._headers())
            if resp.status_code >= 400:
                return []
            data = resp.json().get("data") or []
            return [m["id"] for m in data if m.get("id")]
        except httpx.HTTPError:
            return []

    async def aclose(self) -> None:
        await self._client.aclose()
