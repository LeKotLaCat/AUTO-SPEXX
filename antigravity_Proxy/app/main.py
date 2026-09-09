"""AI Proxy Entrypoint"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .auth import check_key
from .config import Settings, load
from .errors import UpstreamError, SchemaTranslationError
from .session import SessionStore
from .translate import extract_response_format, schema_instruction, strip_json_fences
from .upstream.base import ChatRequest, Upstream
from .upstream.gemini import GeminiUpstream
from .upstream.mock import MockUpstream
from .upstream.openai_compat import OpenAICompatUpstream

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("proxy")


def _make_upstream(settings: Settings, store: SessionStore) -> Upstream:
    if settings.upstream == "mock":
        return MockUpstream(models=settings.models)
    elif settings.upstream == "gemini":
        token_provider = store.get_token if store.configured else None
        return GeminiUpstream(
            base_url=settings.upstream_base_url,
            api_key=settings.upstream_api_key,
            timeout=settings.upstream_timeout_sec,
            token_provider=token_provider,
        )
    elif settings.upstream == "openai":
        return OpenAICompatUpstream(
            base_url=settings.upstream_base_url,
            api_key=settings.upstream_api_key,
            timeout=settings.upstream_timeout_sec,
        )
    raise ValueError(f"unknown upstream {settings.upstream!r}")


def _error_body(message: str, *, etype: str = "invalid_request_error") -> dict:
    return {"error": {"message": message, "type": etype, "param": None, "code": None}}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load()
    store = SessionStore(settings.token_store_path)
    upstream = _make_upstream(settings, store)
    app.state.settings = settings
    app.state.store = store
    app.state.upstream = upstream
    app.state.gate = asyncio.Semaphore(settings.max_concurrency)
    log.info("proxy ready: upstream=%s models=%s schema_mode=%s", settings.upstream, list(settings.models), settings.schema_mode)
    try:
        yield
    finally:
        await app.state.upstream.aclose()


app = FastAPI(title="AI Proxy", version="1.0.0", lifespan=lifespan)


@app.exception_handler(UpstreamError)
async def _upstream_error(request: Request, exc: UpstreamError):
    log.warning("upstream error (%s): %s", exc.kind, exc.message)
    return JSONResponse(status_code=exc.status, content=_error_body(exc.message, etype=f"upstream_{exc.kind}"))


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content=_error_body(str(exc.detail), etype="invalid_request_error"), headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content=_error_body(f"invalid request body: {exc.errors()}", etype="invalid_request_error"))


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content=_error_body(f"proxy internal error: {exc}", etype="proxy_error"))


@app.get("/healthz")
async def healthz(request: Request):
    s = request.app.state.settings
    return {"ok": True, "upstream": s.upstream, "schema_mode": s.schema_mode, "models": list(s.models), "session": request.app.state.store.status()}


@app.get("/v1/models")
async def list_models(request: Request, _key: str = Depends(check_key)):
    now = int(time.time())
    return {
        "object": "list",
        "data": [{"id": alias, "object": "model", "created": now, "owned_by": request.app.state.settings.upstream}
                 for alias in request.app.state.settings.models],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _key: str = Depends(check_key)):
    settings = request.app.state.settings
    upstream = request.app.state.upstream

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "request body must be a JSON object")

    if payload.get("stream"):
        raise HTTPException(400, "streaming is not supported by this proxy")

    alias = str(payload.get("model") or "").strip() or settings.default_model
    if alias not in settings.models:
        raise HTTPException(404, f"model {alias!r} not found — available: {list(settings.models)}")

    raw_messages = payload.get("messages") or []
    if not isinstance(raw_messages, list) or not raw_messages:
        raise HTTPException(400, "messages must be a non-empty array")

    system_parts, messages = [], []
    for m in raw_messages:
        if not isinstance(m, dict):
            raise HTTPException(400, "each message must be an object")
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        content = "" if content is None else str(content)
        if role == "system":
            system_parts.append(content)
        else:
            messages.append({"role": role or "user", "content": content})

    try:
        schema, schema_name = extract_response_format(payload)
    except SchemaTranslationError as exc:
        raise HTTPException(400, str(exc)) from exc

    system_prompt = "\n\n".join(p for p in system_parts if p)
    fallback = schema is not None and not (upstream.supports_native_schema and settings.schema_mode == "native")
    if fallback:
        system_prompt += schema_instruction(schema, schema_name)

    extra = {}
    if payload.get("reasoning_effort"):
        extra["reasoning_effort"] = payload["reasoning_effort"]

    req = ChatRequest(
        model=settings.upstream_model(alias),
        system_prompt=system_prompt,
        messages=messages,
        max_tokens=payload.get("max_tokens") or payload.get("max_completion_tokens"),
        schema=None if fallback else schema,
        schema_name=schema_name,
        extra=extra,
    )

    started = time.perf_counter()
    try:
        async with request.app.state.gate:
            result = await upstream.complete(req)
    except SchemaTranslationError as exc:
        raise HTTPException(400, f"cannot translate schema for upstream: {exc}") from exc

    text = strip_json_fences(result.text) if fallback else result.text
    elapsed = time.perf_counter() - started
    log.info("chat model=%s took=%.2fs in=%d out=%d fallback=%s", alias, elapsed, result.prompt_tokens, result.completion_tokens, fallback)

    message: dict = {"role": "assistant", "content": text}
    if result.refusal:
        message["refusal"] = result.refusal
        message["content"] = None

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": alias,
        "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason, "logprobs": None}],
        "usage": {"prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens, "total_tokens": result.total_tokens},
    }
