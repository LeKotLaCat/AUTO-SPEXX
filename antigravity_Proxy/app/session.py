"""ถือ session/refresh token ของปลายทางไว้เอง"""
from __future__ import annotations

import asyncio
import json
import os
import time
import httpx

from .errors import UpstreamError, describe, permanent, transient

REFRESH_MARGIN_SEC = 60


class SessionStore:
    def __init__(self, path: str, *, token_url: str = "", client_id: str = "",
                 client_secret: str = "", scope: str = ""):
        self._path = path
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._lock = asyncio.Lock()
        self._data: dict = {}
        self._read()

    def _read(self) -> None:
        if not os.path.isfile(self._path):
            self._data = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except Exception:
            self._data = {}

    def _write(self) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self._path)

    @property
    def configured(self) -> bool:
        return bool(self._data.get("refresh_token") or self._data.get("access_token"))

    def seed(self, *, access_token: str = "", refresh_token: str = "", expires_in: int = 0) -> None:
        if access_token:
            self._data["access_token"] = access_token
            self._data["expires_at"] = time.time() + expires_in if expires_in else 0
        if refresh_token:
            self._data["refresh_token"] = refresh_token
        self._write()

    def _fresh_enough(self) -> bool:
        token = self._data.get("access_token")
        if not token:
            return False
        expires_at = self._data.get("expires_at") or 0
        if not expires_at:
            return True
        return time.time() + REFRESH_MARGIN_SEC < expires_at

    async def get_token(self) -> str:
        if self._fresh_enough():
            return self._data["access_token"]
        async with self._lock:
            if self._fresh_enough():
                return self._data["access_token"]
            await self._refresh()
            return self._data.get("access_token", "")

    async def _refresh(self) -> None:
        refresh_token = self._data.get("refresh_token")
        if not refresh_token or not self._token_url:
            access = self._data.get("access_token")
            if access:
                return
            raise permanent("unauthorized: ไม่มี access_token หรือ refresh_token ที่ใช้ได้")

        form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        if self._client_id:
            form["client_id"] = self._client_id
        if self._client_secret:
            form["client_secret"] = self._client_secret
        if self._scope:
            form["scope"] = self._scope

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(self._token_url, data=form)
            except httpx.HTTPError as exc:
                raise transient(f"cannot reach token endpoint, unavailable: {describe(exc)}") from exc

        if resp.status_code in (400, 401, 403):
            raise permanent(f"unauthorized: refresh token ถูกปฏิเสธ ({resp.status_code}) — ต้อง login ใหม่: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise transient(f"token refresh failed with {resp.status_code}, upstream unavailable: {resp.text[:200]}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise transient(f"token endpoint returned non-JSON, unavailable: {exc}") from exc

        access = payload.get("access_token")
        if not access:
            raise permanent("unauthorized: token endpoint ไม่ได้คืน access_token")

        self._data["access_token"] = access
        expires_in = int(payload.get("expires_in") or 0)
        self._data["expires_at"] = time.time() + expires_in if expires_in else 0
        if payload.get("refresh_token"):
            self._data["refresh_token"] = payload["refresh_token"]
        self._write()

    def status(self) -> dict:
        expires_at = self._data.get("expires_at") or 0
        return {
            "has_access_token": bool(self._data.get("access_token")),
            "has_refresh_token": bool(self._data.get("refresh_token")),
            "expires_in_sec": max(0, int(expires_at - time.time())) if expires_at else None,
            "can_refresh": bool(self._token_url and self._data.get("refresh_token")),
        }
