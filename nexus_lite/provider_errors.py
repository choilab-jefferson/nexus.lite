"""nexus_lite.provider_errors — classify a provider CLI/API failure (pure text/JSON, stdlib).

``classify_provider_error(rc, output)`` returns ``'rate_limit' | 'quota' | 'auth' | None``.
No Redis, circuit breaker or ledger. A richer downstream extension (breaker,
per-pool state) lives in nexus.code ``core/load_balancer/quota.py``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

RATE_LIMIT_MARKERS = (
    "rate limit exceeded",
    "rate limit reached",
    "rate_limit_error",
    "rate_limit_exceeded",
    "ratelimitexceeded",
    "too many requests",
    "429 too many requests",
    "http 429",
    "status: 429",
    "status code: 429",
    "error 429",
)

QUOTA_MARKERS = (
    "hit your weekly limit",
    "hit your session limit",
    "hit your daily limit",
    "hit your usage limit",
    "hit your monthly limit",
    "weekly limit reached",
    "daily limit reached",
    "monthly limit reached",
    "free usage limit reached",
    "usage limit reached",
    "freeusagelimiterror",
    "quota exceeded",
    "quota reached",
    "exceeded your current quota",
    "exceeded your quota",
    "resource_exhausted",
    "out of quota",
    "out of credits",
    "model unavailable",
    "providermodelnotfounderror",
)

AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "please sign in",
    "authentication required",
    "invalid api key",
    "unauthorized",
    "401 unauthorized",
    "permission denied",
    "forbidden",
)

_QUOTA_OR_AUTH_MARKERS = RATE_LIMIT_MARKERS + QUOTA_MARKERS + AUTH_MARKERS

_STATUS_KEYS = frozenset(
    {"api_error_status", "status", "status_code", "statuscode", "http_status", "code", "type"}
)
_TOKEN_CLASS = {
    "429": "rate_limit",
    "rate_limit_error": "rate_limit",
    "ratelimitexceeded": "rate_limit",
    "rate_limit_exceeded": "rate_limit",
    "too_many_requests": "rate_limit",
    "resource_exhausted": "quota",
    "insufficient_quota": "quota",
    "quota_exceeded": "quota",
    "401": "auth",
    "403": "auth",
    "authentication_error": "auth",
    "permission_error": "auth",
    "unauthenticated": "auth",
    "permission_denied": "auth",
    "invalid_api_key": "auth",
}


def _json_class(obj: Any, depth: int = 0) -> Optional[str]:
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in _STATUS_KEYS and isinstance(v, (int, str)) and not isinstance(v, bool):
                got = _TOKEN_CLASS.get(str(v).strip().lower())
                if got:
                    return got
            if isinstance(v, (dict, list)):
                got = _json_class(v, depth + 1)
                if got:
                    return got
    elif isinstance(obj, list):
        for v in obj[:20]:
            got = _json_class(v, depth + 1)
            if got:
                return got
    return None


def _text_class(low: str) -> Optional[str]:
    for name, markers in (
        ("rate_limit", RATE_LIMIT_MARKERS),
        ("quota", QUOTA_MARKERS),
        ("auth", AUTH_MARKERS),
    ):
        if any(m in low for m in markers):
            return name
    return None


def classify_provider_error(rc: int, output: str) -> Optional[str]:
    """Return 'rate_limit' | 'quota' | 'auth' | None. rc == 0 is never a failure."""
    if rc == 0:
        return None
    text = (output or "").strip()
    if not text:
        return None
    candidates = [text] + [ln.strip() for ln in text.splitlines()]
    for c in candidates:
        if c.startswith(("{", "[")):
            try:
                got = _json_class(json.loads(c))
            except ValueError:
                continue
            if got:
                return got
    return _text_class(text.lower())


def is_quota_or_auth_failure(rc: int, output: str) -> bool:
    """True if an agent invocation failed due to quota, rate limit, auth, a non-0/3 exit, or empty output."""
    text = (output or "").strip()
    if _text_class(text.lower()):
        return True
    if rc not in (0, 3):
        return True
    return not text
