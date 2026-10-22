import os
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from qaisar_radar.app import create_app, CandleAlert, evaluate

TOKEN = "w" * 40
ADMIN = "a" * 40

def payload():
    return {
        "symbol": "NVDA", "exchange": "NASDAQ", "timeframe": "5",
        "side": "LONG_WATCH",
        "bar_time": int(datetime.now(timezone.utc).timestamp() * 1000),
        "price": 105, "candle_open": 103, "previous_high": 104, "previous_low": 101,
        "volume": 250000, "avg_volume_20": 100000, "vwap": 102,
        "ema20": 103, "ema50": 100, "rsi14": 60,
    }

def test_long_filter():
    assert evaluate(CandleAlert(**payload()))["verdict"] == "LONG_WATCH"
    data = payload()
    data["volume"] = 100
    assert evaluate(CandleAlert(**data))["verdict"] == "REJECT"

def test_short_filter():
    data = payload()
    data.update(side="SHORT_WATCH", price=99, candle_open=101, previous_low=100,
                vwap=102, ema20=99, ema50=101, rsi14=39)
    assert evaluate(CandleAlert(**data))["verdict"] == "SHORT_WATCH"

def test_webhook_security_and_dedup(tmp_path):
    app = create_app(db_path=str(tmp_path / "radar.db"),
                     webhook_token=TOKEN, admin_token=ADMIN, ai_enabled=False)
    with TestClient(app) as client:
        assert client.post("/radar/webhook/wrong", json=payload()).status_code == 403
        one = client.post(f"/radar/webhook/{TOKEN}", json=payload())
        assert one.status_code == 200, one.text
        assert one.json()["analysis"]["verdict"] == "LONG_WATCH"
        assert client.post(f"/radar/webhook/{TOKEN}", json=payload()).json()["status"] == "duplicate"
        assert client.get("/radar/events").status_code == 401
        rows = client.get("/radar/events", headers={"X-Admin-Token": ADMIN}).json()["events"]
        assert len(rows) == 1
        assert "secret" not in str(rows)
        assert client.post(f"/radar/webhook/{TOKEN}", json={**payload(), "symbol": "../bad"}).status_code == 422

def test_no_bad_or_stale_candle(tmp_path):
    app = create_app(db_path=str(tmp_path / "data.db"), webhook_token=TOKEN,
                     admin_token=ADMIN, ai_enabled=False)
    with TestClient(app) as client:
        a = payload()
        a["volume"] = "NaN"
        assert client.post(f"/radar/webhook/{TOKEN}", json=a).status_code == 422
        a = payload()
        a["bar_time"] = 1500000000000
        assert client.post(f"/radar/webhook/{TOKEN}", json=a).status_code == 422
