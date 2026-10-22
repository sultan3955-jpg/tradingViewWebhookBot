import asyncio
import json
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qaisar_security import secure_equal, redact_sensitive, read_limited_body, safe_json_text
from apply_qaisar_security import patch_main


def test_secure_equal_rejects_invalid_and_accepts_same():
    assert secure_equal('a' * 32, 'a' * 32)
    assert not secure_equal('a' * 32, 'b' * 32)
    assert not secure_equal(None, 'a' * 32)
    assert not secure_equal('', '')


def test_recursive_redaction_preserves_trade_values():
    raw = {'secret': 'TOP-SECRET', 'symbol': 'NVDA', 'price': '57',
           'nested': [{'api_key': 'API-SECRET', 'position_size': '3'}]}
    clean = redact_sensitive(raw)
    output = safe_json_text(raw)
    assert 'TOP-SECRET' not in output and 'API-SECRET' not in output
    assert clean['symbol'] == 'NVDA'
    assert clean['nested'][0]['position_size'] == '3'


def test_upstream_patch_rejects_unexpected_source():
    with pytest.raises(RuntimeError, match='Upstream mismatch'):
        patch_main('this is a different main.py')


def test_webhook_payload_limit():
    app = FastAPI()
    @app.post('/body')
    async def body(req: Request):
        data = await read_limited_body(req, limit=4)
        return {'text': data.decode('utf-8')}
    with TestClient(app) as client:
        assert client.post('/body', data=b'1234').status_code == 200
        assert client.post('/body', data=b'12345').status_code == 413


def test_case_insensitive_sensitive_keys_and_nested_arrays():
    raw = {"Secret": "danger", "levels": [[{"Authorization": "bearer-value"}]], "symbol": "NVDA"}
    redacted = redact_sensitive(raw)
    assert redacted["Secret"] == "[REDACTED]"
    assert redacted["levels"][0][0]["Authorization"] == "[REDACTED]"
    assert redacted["symbol"] == "NVDA"


def test_depth_limited_redaction():
    deep = {"level": 1}
    for _ in range(20):
        deep = {"nested": deep}
    result = safe_json_text(deep)
    assert "TRUNCATED" in result


def test_health_and_private_route_security_scaffolding():
    # Tests required patch anchors without assuming upstream's main.py is available here.
    original = Path(__file__).resolve().parents[1] / 'main.py'
    if not original.exists():
        pytest.skip('Run after patching inside the cloned project to validate main.py')
    src = original.read_text(encoding='utf-8')
    assert 'Qaisar security hardening v1' in src
    assert 'protect_private_routes' in src
    assert 'raw_payload_to_store = safe_json_text' in src
