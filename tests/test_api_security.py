"""Exercise real application routes with isolated storage and test credentials."""
import importlib
import json
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WEBHOOK_SECRET', 'w' * 40)
    monkeypatch.setenv('ADMIN_API_TOKEN', 'a' * 40)
    sys.modules.pop('main', None)
    app_module = importlib.import_module('main')
    with TestClient(app_module.app) as test_client:
        yield test_client
    sys.modules.pop('main', None)


def alert(**updates):
    payload = dict(secret='w' * 40, source='test', action='LONG', symbol='NVDA',
                   price='140', timeframe='5', exchange='NASDAQ',
                   timestamp='2026-10-22T13:30:00Z')
    payload.update(updates)
    return payload


@pytest.mark.parametrize('path,method', [('/signals','get'), ('/stats','get'),
    ('/performance','get'), ('/performance/export','get'), ('/performance/json','get'),
    ('/trade_reconciliation','post'), ('/docs','get'), ('/openapi.json','get')])
def test_private_routes_deny_missing_or_wrong_token(client, path, method):
    request = getattr(client, method)
    assert request(path).status_code == 401
    assert request(path, headers={'X-Admin-Token':'wrong'}).status_code == 401


def test_valid_alert_redacts_storage_and_private_responses(client):
    assert client.get('/health').status_code == 200
    payload = alert(nested={'Api_Key':'do-not-store'})
    assert client.post('/webhook', json=payload).status_code == 201
    assert client.post('/webhook', json=payload).status_code == 200
    headers = {'X-Admin-Token':'a' * 40}
    response = client.get('/signals', headers=headers)
    assert response.status_code == 200
    assert response.json()['count'] == 1
    with sqlite3.connect('signals.db') as db:
        saved = db.execute('SELECT raw_payload FROM signals').fetchone()[0]
    from pathlib import Path
    for text in (saved, Path('trade_signals.csv').read_text(), response.text):
        assert 'w' * 40 not in text
        assert 'do-not-store' not in text
        assert '[REDACTED]' in text
    for path in ('/stats', '/performance', '/performance/export', '/performance/json'):
        assert client.get(path, headers=headers).status_code == 200


@pytest.mark.parametrize('payload,code', [(alert(secret='wrong'),403),
    ([],400), (None,400), ({},401), (alert(price=None),400),
    (alert(order_action='buy',position_size='bad'),400)])
def test_invalid_requests(client, payload, code):
    assert client.post('/webhook', content=json.dumps(payload)).status_code == code


def test_invalid_encoding_and_large_body(client):
    assert client.post('/webhook', content=b'\xff').status_code == 400
    assert client.post('/webhook', content=b'{' ).status_code == 400
    assert client.post('/webhook', content=b'x' * (64 * 1024 + 1)).status_code == 413


def test_strategy_payload_nested_redaction(client):
    payload = alert(order_action='buy', order_contracts='1', order_price='140', position_size='1')
    payload.pop('action')
    assert client.post('/webhook', json=payload).status_code == 201
    response = client.get('/signals', headers={'X-Admin-Token':'a' * 40})
    stored = json.loads(response.json()['signals'][0]['raw_payload'])
    assert stored['original_strategy_payload']['secret'] == '[REDACTED]'
    assert stored['normalized_bot_payload']['secret'] == '[REDACTED]'


def test_reconciliation_upload_errors(client):
    headers = {'X-Admin-Token':'a' * 40}
    assert client.post('/trade_reconciliation', headers=headers,
                       files={'file':('bad.csv', b'\xff')}).status_code == 400
    assert client.post('/trade_reconciliation', headers=headers,
                       files={'file':('big.csv', b'x' * (1024 * 1024 + 1))}).status_code == 413


@pytest.mark.parametrize('webhook,admin', [('short','a' * 40), ('w' * 40,''), ('w' * 40,'w' * 40)])
def test_startup_rejects_weak_or_shared_credentials(tmp_path, monkeypatch, webhook, admin):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WEBHOOK_SECRET', webhook)
    monkeypatch.setenv('ADMIN_API_TOKEN', admin)
    sys.modules.pop('main', None)
    with pytest.raises(ValueError):
        importlib.import_module('main')
