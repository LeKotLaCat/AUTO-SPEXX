"""อ่านค่าตั้งจาก environment — ที่เดียวที่รู้ว่า "ปลายทางจริง" คืออะไร"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    raw = os.environ.get(key, default).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1].strip()
    return raw


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class ConfigError(RuntimeError):
    """ค่าตั้งผิด — ต้องล้มตั้งแต่ตอนบูต"""


def _parse_models(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        alias, _, upstream = chunk.partition("=")
        alias = alias.strip()
        upstream = upstream.strip() or alias
        if not alias:
            raise ConfigError(f"empty model alias in PROXY_MODELS: {raw!r}")
        out[alias] = upstream
    if not out:
        raise ConfigError("PROXY_MODELS must list at least one model")
    return out


@dataclass(frozen=True)
class Settings:
    api_keys: frozenset = frozenset()
    models: dict = field(default_factory=dict)
    upstream: str = "mock"
    upstream_base_url: str = ""
    upstream_api_key: str = ""
    upstream_timeout_sec: int = 120
    max_concurrency: int = 4
    schema_mode: str = "native"
    token_store_path: str = "/data/session.json"
    log_bodies: bool = False

    @property
    def default_model(self) -> str:
        return next(iter(self.models))

    def upstream_model(self, alias: str) -> str:
        return self.models[alias]


def load() -> Settings:
    keys = frozenset(k.strip() for k in _env("PROXY_API_KEYS").split(",") if k.strip())
    if not keys:
        raise ConfigError("PROXY_API_KEYS is required")

    upstream = _env("PROXY_UPSTREAM", "mock").lower()
    schema_mode = _env("PROXY_SCHEMA_MODE", "native").lower()
    if schema_mode not in ("native", "prompt"):
        raise ConfigError(f"PROXY_SCHEMA_MODE must be native|prompt, got {schema_mode!r}")

    models = _parse_models(_env("PROXY_MODELS", "speexx-solver=gemini-2.5-flash"))

    return Settings(
        api_keys=keys,
        models=models,
        upstream=upstream,
        upstream_base_url=_env("PROXY_UPSTREAM_BASE_URL"),
        upstream_api_key=_env("PROXY_UPSTREAM_API_KEY"),
        upstream_timeout_sec=_env_int("PROXY_UPSTREAM_TIMEOUT_SEC", 120),
        max_concurrency=_env_int("PROXY_MAX_CONCURRENCY", 4),
        schema_mode=schema_mode,
        token_store_path=_env("PROXY_TOKEN_STORE", "/data/session.json"),
        log_bodies=_env_bool("PROXY_LOG_BODIES", False),
    )
