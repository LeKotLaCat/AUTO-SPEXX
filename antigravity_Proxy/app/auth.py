"""ตรวจ Bearer token ขาเข้า

ถึงจะรันในวง Tailscale ก็ยังต้องตรวจ
ไม่ตรวจ = ใครก็ตามที่เข้าถึงเครื่องได้ใช้โควตาได้ฟรี
"""
from __future__ import annotations

import hmac
from fastapi import Header, HTTPException, Request


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=f"unauthorized: {detail}",
        headers={"WWW-Authenticate": "Bearer"},
    )


def check_key(request: Request, authorization: str = Header(default="")) -> str:
    settings = request.app.state.settings

    if not authorization:
        raise _unauthorized("missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("expected 'Authorization: Bearer <key>'")

    token = token.strip()
    ok = False
    for known in settings.api_keys:
        if hmac.compare_digest(token, known):
            ok = True
    if not ok:
        raise _unauthorized("invalid api key")
    return token
