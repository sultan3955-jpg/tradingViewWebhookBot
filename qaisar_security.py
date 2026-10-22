"""Qaisar security helpers: fail-closed access control and alert redaction.

Does not perform trades or connect to a broker.
"""

import hmac
import json
from collections.abc import Mapping

from fastapi import HTTPException, Request

WEBHOOK_MAX_BYTES = 64 * 1024
RECONCILIATION_MAX_BYTES = 1024 * 1024
_SENSITIVE = {
    'secret', 'password', 'token', 'access_token', 'refresh_token',
    'api_key', 'apikey', 'api_secret', 'authorization', 'x-admin-token',
    'webhook_secret', 'admin_api_token', 'private_key',
}


def secure_equal(a: object, b: object) -> bool:
    """Constant-time compare after type and empty-value checks."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


def redact_sensitive(value, *, depth=0):
    """Preserve non-secret alert data for performance analytics."""
    if depth > 12:
        return '[TRUNCATED]'
    if isinstance(value, Mapping):
        return {
            str(k): ('[REDACTED]' if str(k).lower() in _SENSITIVE else redact_sensitive(v, depth=depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(v, depth=depth + 1) for v in value]
    return value


async def read_limited_body(request: Request, limit: int = WEBHOOK_MAX_BYTES) -> bytes:
    """Bound webhook size while streaming; prevent large in-memory bodies."""
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail='Payload too large')
        chunks.append(chunk)
    return b''.join(chunks)


def safe_json_text(value) -> str:
    return json.dumps(redact_sensitive(value), ensure_ascii=False, separators=(',', ':'))
