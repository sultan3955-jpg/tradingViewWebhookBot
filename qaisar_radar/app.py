"""Qaisar US Stock Radar: TradingView -> volume/technical filters -> optional OpenAI.
Signals only. No broker access or automatic orders.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_BODY = 16 * 1024
MIN_VOLUME = 100_000
MIN_RVOL = 1.8


class CandleAlert(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,14}$")
    exchange: str = Field(min_length=2, max_length=25, pattern=r"^[A-Z_]+$")
    timeframe: Literal["5"]
    side: Literal["LONG_WATCH", "SHORT_WATCH"]
    bar_time: int = Field(ge=1_500_000_000_000, le=4_100_000_000_000)
    price: float = Field(gt=0)
    candle_open: float = Field(gt=0)
    previous_high: float = Field(gt=0)
    previous_low: float = Field(gt=0)
    volume: float = Field(ge=0)
    avg_volume_20: float = Field(gt=0)
    vwap: float = Field(gt=0)
    ema20: float = Field(gt=0)
    ema50: float = Field(gt=0)
    rsi14: float = Field(ge=0, le=100)


def evaluate(c: CandleAlert) -> dict:
    """Deterministic checks; volume is shares PER 5-minute candle, not buy/sell delta."""
    rvol = c.volume / c.avg_volume_20
    long_ok = all((
        c.price > c.vwap, c.ema20 > c.ema50, c.rsi14 >= 50,
        c.price > c.candle_open, c.price > c.previous_high,
    ))
    short_ok = all((
        c.price < c.vwap, c.ema20 < c.ema50, c.rsi14 <= 50,
        c.price < c.candle_open, c.price < c.previous_low,
    ))
    ok = c.volume >= MIN_VOLUME and rvol >= MIN_RVOL and (
        long_ok if c.side == "LONG_WATCH" else short_ok
    )
    verdict = c.side if ok else "REJECT"
    return {
        "verdict": verdict,
        "symbol": c.symbol,
        "side_requested": c.side,
        "timeframe": c.timeframe,
        "bar_time": c.bar_time,
        "price": c.price,
        "volume_shares_5m": c.volume,
        "avg_volume_shares_previous_20_bars": c.avg_volume_20,
        "rvol_20": round(rvol, 3),
        "approx_dollar_turnover_5m": round(c.price * c.volume, 2),
        "vwap": c.vwap,
        "vwap_distance_pct": round((c.price / c.vwap - 1) * 100, 3),
        "ema20": c.ema20,
        "ema50": c.ema50,
        "rsi14": c.rsi14,
        "note_ar": (
            "الكمية هي حجم تداول الشمعة وليست عدد أوامر الشراء أو البيع الفعلية. "
            "RVOL مقارنة بمتوسط آخر 20 شمعة مكتملة، وليس نفس التوقيت من الأيام السابقة."
        ),
    }


def _extract_output_text(data: dict) -> str:
    parts = []
    for item in data.get("output", []):
        for section in item.get("content", []):
            if section.get("type") == "output_text":
                parts.append(section.get("text", ""))
    return "\n".join(parts).strip()[:6000]


def create_app(
    *,
    db_path: str | None = None,
    webhook_token: str | None = None,
    admin_token: str | None = None,
    ai_enabled: bool | None = None,
) -> FastAPI:
    database = db_path or os.getenv("QAISAR_DB_PATH", "qaisar_radar/events.db")
    webhook_key = webhook_token if webhook_token is not None else os.getenv("QAISAR_WEBHOOK_TOKEN", "")
    admin_key = admin_token if admin_token is not None else os.getenv("QAISAR_ADMIN_TOKEN", "")
    ai_on = ai_enabled if ai_enabled is not None else os.getenv("QAISAR_AI_ENABLED", "false").lower() == "true"
    ai_key = os.getenv("OPENAI_API_KEY", "")
    ai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if min(len(webhook_key), len(admin_key)) < 32 or hmac.compare_digest(webhook_key, admin_key):
            raise RuntimeError("Configure two different random tokens, at least 32 characters each")
        if ai_on and not ai_key:
            raise RuntimeError("OPENAI_API_KEY required only when QAISAR_AI_ENABLED=true")
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                verdict TEXT NOT NULL,
                volume REAL NOT NULL,
                rvol REAL NOT NULL,
                analysis_json TEXT NOT NULL,
                ai_status TEXT NOT NULL,
                ai_report TEXT
                )"""
            )
        yield

    app = FastAPI(title="Qaisar US Stock Radar", version="1.0.0", lifespan=lifespan)

    async def generate_ai_report(event_id: str, analysis: dict) -> None:
        """Runs AFTER the webhook response; never blocks TradingView's 3-second window."""
        report = ""
        state = "failed"
        try:
            async with httpx.AsyncClient(timeout=18) as client:
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {ai_key}"},
                    json={
                        "model": ai_model,
                        "instructions": (
                            "You are an Arabic market-research assistant. Explain the supplied "
                            "deterministic indicators, identify uncertainty and invalidation, "
                            "and avoid definitive trade advice. Do not invent live prices, "
                            "options flow, bid/ask delta or further data. 140 words maximum."
                        ),
                        "input": json.dumps(analysis, ensure_ascii=False),
                        "max_output_tokens": 350,
                    },
                )
                response.raise_for_status()
                report = _extract_output_text(response.json())
                state = "done" if report else "failed"
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            state = "failed"
        with sqlite3.connect(database) as con:
            con.execute(
                "UPDATE events SET ai_status=?, ai_report=? WHERE event_id=?",
                (state, report or None, event_id),
            )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/radar/webhook/{token}")
    async def webhook(token: str, request: Request, tasks: BackgroundTasks):
        if not hmac.compare_digest(token, webhook_key):
            raise HTTPException(status_code=403, detail="Forbidden")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(status_code=413, detail="Payload too large")
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            alert = CandleAlert.model_validate(data)
        except (ValueError, UnicodeError, ValidationError, TypeError):
            raise HTTPException(status_code=422, detail="Invalid TradingView candle alert") from None

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # Reject delayed/replayed alerts, with a small allowance for clock skew.
        if not (-5 * 60_000 <= now_ms - alert.bar_time <= 30 * 60_000):
            raise HTTPException(status_code=422, detail="Stale or future candle")
        result = evaluate(alert)
        event_id = hashlib.sha256(
            f"{alert.exchange}:{alert.symbol}:{alert.timeframe}:{alert.bar_time}:{alert.side}".encode()
        ).hexdigest()
        with sqlite3.connect(database, timeout=3) as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO events
                (event_id, received_at, symbol, side, verdict, volume, rvol,
                 analysis_json, ai_status, ai_report)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    event_id, datetime.now(timezone.utc).isoformat(), alert.symbol,
                    alert.side, result["verdict"], alert.volume, result["rvol"],
                    json.dumps(result, ensure_ascii=False),
                    "queued" if ai_on else "disabled",
                ),
            )
            inserted = cur.rowcount == 1
        if inserted and ai_on:
            tasks.add_task(generate_ai_report, event_id, result)
        return {
            "status": "received" if inserted else "duplicate",
            "event_id": event_id,
            "analysis": result,
            "ai_status": ("queued" if ai_on else "disabled") if inserted else "unchanged",
        }

    @app.get("/radar/events")
    def events(limit: int = 20, x_admin_token: str | None = Header(default=None)):
        if not x_admin_token or not hmac.compare_digest(x_admin_token, admin_key):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="limit must be 1..100")
        with sqlite3.connect(database) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return {
            "events": [
                {
                    "event_id": row["event_id"],
                    "received_at": row["received_at"],
                    "analysis": json.loads(row["analysis_json"]),
                    "ai_status": row["ai_status"],
                    "ai_report": row["ai_report"],
                }
                for row in rows
            ]
        }

    return app


app = create_app()
