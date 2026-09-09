"""แปลงความล้มเหลวของปลายทาง -> ข้อความที่เข้าใจได้"""
from __future__ import annotations

TRANSIENT_MARKERS = ("503", "unavailable", "429", "resource_exhausted",
                     "overloaded", "rate limit", "timeout", "timed out", "deadline")
PERMANENT_MARKERS = ("spending cap", "billing", "quota exceeded", "exceeded its monthly",
                     "invalid api key", "unauthorized", "permission denied",
                     "api key not valid")


class UpstreamError(Exception):
    def __init__(self, message: str, *, kind: str = "other", status: int = 502):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status


def describe(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _has(text: str, markers) -> bool:
    low = text.lower()
    return any(m in low for m in markers)


def transient(detail: str, *, status: int = 503) -> UpstreamError:
    msg = detail if _has(detail, TRANSIENT_MARKERS) else f"upstream unavailable: {detail}"
    return UpstreamError(msg, kind="transient", status=status)


def permanent(detail: str, *, status: int = 502) -> UpstreamError:
    msg = detail if _has(detail, PERMANENT_MARKERS) else f"permission denied: {detail}"
    return UpstreamError(msg, kind="permanent", status=status)


def classify_upstream_status(status: int, body: str) -> UpstreamError:
    body = (body or "").strip()
    snippet = body[:400]
    quota_words = ("quota", "exceeded your current quota", "spending", "billing", "exhausted", "insufficient_quota", "credit")

    if status in (401, 403):
        if "quota" in body.lower() or "billing" in body.lower():
            return permanent(f"quota exceeded (upstream {status}): {snippet}")
        return permanent(f"unauthorized (upstream {status}): {snippet}")

    if status == 429:
        if _has(body, quota_words):
            return permanent(f"429 quota exceeded — upstream หมดโควตา/เพดานเงิน: {snippet}")
        return transient(f"429 rate limit from upstream: {snippet}", status=429)

    if status == 404:
        return UpstreamError(f"upstream endpoint or model not found (404): {snippet}", kind="other", status=502)

    if status in (408, 502, 503, 504):
        return transient(f"{status} upstream unavailable: {snippet}", status=503)

    if 500 <= status < 600:
        return transient(f"{status} upstream error, overloaded: {snippet}", status=503)

    if status == 400:
        return UpstreamError(f"upstream rejected the request (400): {snippet}", kind="other", status=502)

    return UpstreamError(f"upstream returned {status}: {snippet}", kind="other", status=502)
