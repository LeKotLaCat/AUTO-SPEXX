"""ปลายทาง Gemini (generateContent)"""
from __future__ import annotations

import httpx

from ..errors import UpstreamError, classify_upstream_status, describe, transient
from ..translate import to_gemini_schema
from .base import ChatRequest, Completion, Upstream

DEFAULT_BASE = "https://generativelanguage.googleapis.com"

_BAD_FINISH = {
    "SAFETY": "blocked by upstream safety filter",
    "RECITATION": "blocked as recitation",
    "PROHIBITED_CONTENT": "blocked as prohibited content",
    "BLOCKLIST": "blocked by upstream blocklist",
}


class GeminiUpstream(Upstream):
    name = "gemini"
    supports_native_schema = True

    def __init__(self, *, base_url: str, api_key: str, timeout: int, token_provider=None):
        self._base = (base_url or DEFAULT_BASE).rstrip("/")
        self._api_key = api_key
        self._token_provider = token_provider
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )

    async def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._token_provider is not None:
            h["Authorization"] = f"Bearer {await self._token_provider()}"
        elif self._api_key:
            h["x-goog-api-key"] = self._api_key
        return h

    def _build_body(self, req: ChatRequest) -> dict:
        contents = []
        for m in req.messages:
            role = "model" if m.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": str(m.get("content", ""))}]})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": ""}]}]

        gen: dict = {}
        if req.max_tokens:
            gen["maxOutputTokens"] = req.max_tokens
        if req.schema is not None:
            gen["responseMimeType"] = "application/json"
            if req.schema:
                gen["responseSchema"] = to_gemini_schema(req.schema)

        effort = str(req.extra.get("reasoning_effort", "")).lower()
        if effort in ("none", "minimal"):
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        elif effort == "low":
            gen["thinkingConfig"] = {"thinkingBudget": 512}

        body: dict = {"contents": contents}
        if gen:
            body["generationConfig"] = gen
        if req.system_prompt:
            body["systemInstruction"] = {"parts": [{"text": req.system_prompt}]}
        return body

    async def complete(self, req: ChatRequest) -> Completion:
        url = f"{self._base}/v1beta/models/{req.model}:generateContent"
        try:
            resp = await self._client.post(url, json=self._build_body(req), headers=await self._headers())
        except httpx.TimeoutException as exc:
            raise transient(f"upstream timeout: {describe(exc)}") from exc
        except httpx.HTTPError as exc:
            raise transient(f"cannot reach upstream, unavailable: {describe(exc)}") from exc

        if resp.status_code >= 400:
            raise classify_upstream_status(resp.status_code, resp.text)

        return self._parse(resp.json())

    def _parse(self, data: dict) -> Completion:
        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            return Completion(text="", refusal=f"upstream blocked the prompt: {feedback['blockReason']}", finish_reason="content_filter")

        candidates = data.get("candidates") or []
        if not candidates:
            raise UpstreamError("upstream returned no candidates", kind="other")

        cand = candidates[0]
        finish = str(cand.get("finishReason") or "STOP").upper()
        if finish in _BAD_FINISH:
            return Completion(text="", refusal=_BAD_FINISH[finish], finish_reason="content_filter")

        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))

        usage = data.get("usageMetadata") or {}
        prompt_tok = int(usage.get("promptTokenCount") or 0)
        out_tok = int(usage.get("candidatesTokenCount") or 0)
        out_tok += int(usage.get("thoughtsTokenCount") or 0)

        if not text.strip() and finish == "MAX_TOKENS":
            raise UpstreamError("upstream hit max_tokens before producing an answer", kind="other")

        return Completion(text=text, prompt_tokens=prompt_tok, completion_tokens=out_tok,
                          finish_reason="length" if finish == "MAX_TOKENS" else "stop")

    async def list_models(self) -> list:
        return []

    async def aclose(self) -> None:
        await self._client.aclose()
