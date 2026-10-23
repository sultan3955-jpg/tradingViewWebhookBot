#!/usr/bin/env python3
"""
TradingView Webhook Bot - Greedy Strategy Edition

Supports BOTH:
1. Indicator alerts:
   LONG, SHORT, LONG_ADD, SHORT_ADD, CLOSE_ALL

2. Strategy alerts:
   order_action, order_contracts, order_price, position_size,
   order_id, order_comment

Logs normalized signals to SQLite + CSV and calculates forward-test performance.
Includes trade reconciliation against TradingView Strategy Tester CSV exports.
"""

import os
import io
import json
import csv
import sqlite3
import hashlib
import hmac
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status, UploadFile, File, Header, Depends
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, validator
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    raise ValueError("WEBHOOK_SECRET environment variable is required")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
if not ADMIN_TOKEN:
    raise ValueError("ADMIN_TOKEN environment variable is required")

DATABASE_FILE = "signals.db"
CSV_FILE = "trade_signals.csv"
PERFORMANCE_CSV_FILE = "performance_trades.csv"
PERFORMANCE_JSON_FILE = "performance_trades.json"
DUPLICATE_WINDOW_SECONDS = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    yield


app = FastAPI(
    title="TradingView Webhook Bot - Greedy Strategy",
    description="Receives indicator or strategy alerts from TradingView",
    version="3.1.1",
    lifespan=lifespan,
)


def require_admin(x_admin_token: Optional[str] = Header(default=None)):
    if not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token")


class WebhookPayload(BaseModel):
    secret: str
    source: str
    action: str
    symbol: str
    price: str
    timeframe: str
    exchange: str
    timestamp: str
    order_id: Optional[str] = None
    order_comment: Optional[str] = None

    @validator("action")
    def validate_action(cls, v):
        allowed = {"LONG", "SHORT", "LONG_ADD", "SHORT_ADD", "CLOSE_ALL"}
        if v.upper() not in allowed:
            raise ValueError(f"action must be one of: {allowed}")
        return v.upper()


class StrategyPayload(BaseModel):
    secret: str
    source: str

    order_action: str
    order_contracts: str
    order_price: str
    order_id: Optional[str] = None
    order_comment: Optional[str] = None

    market_position: Optional[str] = None
    market_position_size: Optional[str] = None

    prev_market_position: Optional[str] = None
    prev_market_position_size: Optional[str] = None

    position_size: str

    symbol: str
    timeframe: str
    exchange: str
    timestamp: str


class HealthResponse(BaseModel):
    status: str


def init_database():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            source TEXT NOT NULL,
            action TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            exchange TEXT NOT NULL,
            alert_timestamp TEXT NOT NULL,
            raw_payload TEXT NOT NULL,
            duplicate_hash TEXT NOT NULL
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_received_at ON signals(received_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_hash ON signals(duplicate_hash, received_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbol_timeframe_source ON signals(symbol, timeframe, source)")

    conn.commit()
    conn.close()

    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "received_at", "source", "action", "symbol",
                "price", "timeframe", "exchange", "alert_timestamp", "raw_payload"
            ])


def generate_duplicate_hash(source: str, action: str, symbol: str, timeframe: str, price: str) -> str:
    data = f"{source}:{action}:{symbol}:{timeframe}:{price}"
    return hashlib.md5(data.encode()).hexdigest()


def is_duplicate(source: str, action: str, symbol: str, timeframe: str, price: str) -> bool:
    duplicate_hash = generate_duplicate_hash(source, action, symbol, timeframe, price)
    cutoff_time = (datetime.now(timezone.utc) - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)).isoformat()

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM signals WHERE duplicate_hash = ? AND received_at > ?",
        (duplicate_hash, cutoff_time),
    )
    count = cursor.fetchone()[0]
    conn.close()

    return count > 0


def log_to_database(payload: WebhookPayload, raw_payload: str) -> int:
    received_at = datetime.now(timezone.utc).isoformat()
    duplicate_hash = generate_duplicate_hash(
        payload.source, payload.action, payload.symbol, payload.timeframe, payload.price
    )

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO signals
        (received_at, source, action, symbol, price, timeframe, exchange,
         alert_timestamp, raw_payload, duplicate_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        received_at, payload.source, payload.action, payload.symbol,
        payload.price, payload.timeframe, payload.exchange,
        payload.timestamp, raw_payload, duplicate_hash
    ))

    signal_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return signal_id


def append_to_csv(signal_id: int, payload: WebhookPayload, raw_payload: str):
    received_at = datetime.now(timezone.utc).isoformat()
    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            signal_id, received_at, payload.source, payload.action,
            payload.symbol, payload.price, payload.timeframe,
            payload.exchange, payload.timestamp, raw_payload
        ])


def get_current_open_position_size(symbol: str, timeframe: str, source: Optional[str] = None) -> int:
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = """
        SELECT action
        FROM signals
        WHERE symbol = ? AND timeframe = ?
    """
    params = [symbol, timeframe]

    if source:
        query += " AND source = ?"
        params.append(source)

    query += " ORDER BY received_at ASC"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    position = 0

    for row in rows:
        action = row["action"]

        if action == "LONG":
            position = 1
        elif action == "LONG_ADD":
            position += 1
        elif action == "SHORT":
            position = -1
        elif action == "SHORT_ADD":
            position -= 1
        elif action == "CLOSE_ALL":
            position = 0

    return position


def convert_strategy_to_bot_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    strategy_payload = StrategyPayload(**data)

    order_action = strategy_payload.order_action.lower()
    position_size = float(strategy_payload.position_size)
    previous_position = get_current_open_position_size(
        strategy_payload.symbol,
        strategy_payload.timeframe,
        strategy_payload.source,
    )

    if position_size == 0:
        action = "CLOSE_ALL"

    elif order_action == "buy" and position_size > 0:
        action = "LONG_ADD" if previous_position > 0 else "LONG"

    elif order_action == "sell" and position_size < 0:
        action = "SHORT_ADD" if previous_position < 0 else "SHORT"

    else:
        action = "CLOSE_ALL"

    return {
        "secret": strategy_payload.secret,
        "source": strategy_payload.source,
        "action": action,
        "symbol": strategy_payload.symbol,
        "price": strategy_payload.order_price,
        "timeframe": strategy_payload.timeframe,
        "exchange": strategy_payload.exchange,
        "timestamp": strategy_payload.timestamp,
        "order_id": strategy_payload.order_id,
        "order_comment": strategy_payload.order_comment,
    }


@app.post("/webhook", status_code=status.HTTP_201_CREATED)
async def receive_webhook(request: Request):
    try:
        raw_body = await request.body()
        original_raw_payload = raw_body.decode("utf-8")

        try:
            data = json.loads(original_raw_payload)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        if "secret" not in data:
            raise HTTPException(status_code=401, detail="Missing secret")

        if data.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret")

        if "order_action" in data and "position_size" in data:
            normalized_data = convert_strategy_to_bot_payload(data)
            raw_payload_to_store = json.dumps({
                "original_strategy_payload": data,
                "normalized_bot_payload": normalized_data,
            })
        else:
            normalized_data = data
            raw_payload_to_store = original_raw_payload

        try:
            payload = WebhookPayload(**normalized_data)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")

        if is_duplicate(payload.source, payload.action, payload.symbol, payload.timeframe, payload.price):
            return JSONResponse(
                status_code=200,
                content={
                    "status": "ignored",
                    "reason": "duplicate_detected",
                    "action": payload.action,
                    "symbol": payload.symbol,
                },
            )

        signal_id = log_to_database(payload, raw_payload_to_store)
        append_to_csv(signal_id, payload, raw_payload_to_store)

        return JSONResponse(
            status_code=201,
            content={
                "status": "success",
                "id": signal_id,
                "source": payload.source,
                "action": payload.action,
                "symbol": payload.symbol,
                "price": payload.price,
                "message": "Signal logged successfully",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse(status="ok")


@app.get("/signals", dependencies=[Depends(require_admin)])
async def get_signals(limit: int = 100):
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM signals ORDER BY received_at DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return {"signals": [dict(row) for row in rows], "count": len(rows)}


SYMBOL_MULTIPLIERS = {
    "MNQ1!": 2,
    "NQ1!": 20,
    "MES1!": 5,
    "ES1!": 50,
    "RTY1!": 50,
    "M2K1!": 5,
    "MYM1!": 0.50,
    "YM1!": 5,
}


def get_multiplier(symbol: str) -> float:
    return SYMBOL_MULTIPLIERS.get(symbol.upper(), 1)


@app.get("/stats", dependencies=[Depends(require_admin)])
async def get_stats():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as count FROM signals")
        total_signals = cursor.fetchone()["count"]

        cursor.execute("SELECT action, COUNT(*) as count FROM signals GROUP BY action")
        action_counts = {row["action"]: row["count"] for row in cursor.fetchall()}

        cursor.execute("SELECT source, COUNT(*) as count FROM signals GROUP BY source")
        sources = {row["source"]: row["count"] for row in cursor.fetchall()}

        cursor.execute("SELECT symbol, COUNT(*) as count FROM signals GROUP BY symbol")
        symbols = {row["symbol"]: row["count"] for row in cursor.fetchall()}

        cursor.execute("SELECT * FROM signals ORDER BY received_at DESC LIMIT 1")
        last = cursor.fetchone()
        conn.close()

        return {
            "status": "ok",
            "total_signals": total_signals,
            "long_signals": action_counts.get("LONG", 0),
            "short_signals": action_counts.get("SHORT", 0),
            "long_add_signals": action_counts.get("LONG_ADD", 0),
            "short_add_signals": action_counts.get("SHORT_ADD", 0),
            "close_all_signals": action_counts.get("CLOSE_ALL", 0),
            "sources": sources,
            "symbols": symbols,
            "last_signal": dict(last) if last else None,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stats error: {str(e)}")


def write_performance_csv(closed_trades):
    with open(PERFORMANCE_CSV_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "trade_id", "symbol", "timeframe", "side", "contracts",
            "entry_price", "exit_price", "points", "multiplier", "pnl",
            "entry_time", "exit_time"
        ])

        for idx, trade in enumerate(closed_trades, start=1):
            writer.writerow([
                idx, trade["symbol"], trade["timeframe"], trade["side"], trade["contracts"],
                trade["entry_price"], trade["exit_price"], trade["points"],
                trade["multiplier"], trade["pnl"], trade["entry_time"], trade["exit_time"]
            ])


def write_performance_json(closed_trades):
    pnl_values = [float(t["pnl"]) for t in closed_trades]
    wins = [p for p in pnl_values if p > 0]
    losses = [p for p in pnl_values if p < 0]

    closed_count = len(closed_trades)
    net_profit = round(sum(pnl_values), 2)
    win_rate = round((len(wins) / closed_count) * 100, 2) if closed_count > 0 else 0

    trade_list = []

    for idx, trade in enumerate(closed_trades, start=1):
        pnl = float(trade["pnl"])
        result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN"

        trade_list.append({
            "trade_id": idx,
            "symbol": trade["symbol"],
            "timeframe": trade["timeframe"],
            "side": trade["side"],
            "contracts": trade["contracts"],
            "entry_price": float(trade["entry_price"]),
            "exit_price": float(trade["exit_price"]),
            "points": round(float(trade["points"]), 2),
            "multiplier": trade["multiplier"],
            "pnl": round(pnl, 2),
            "result": result,
            "entry_time": trade["entry_time"],
            "exit_time": trade["exit_time"],
        })

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "closed_trades": closed_count,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "net_profit": net_profit,
        },
        "trades": trade_list,
    }

    with open(PERFORMANCE_JSON_FILE, "w") as f:
        json.dump(report, f, indent=4)


@app.get("/performance/export", dependencies=[Depends(require_admin)])
async def export_performance_csv():
    if not os.path.exists(PERFORMANCE_CSV_FILE):
        write_performance_csv([])
    return FileResponse(PERFORMANCE_CSV_FILE, media_type="text/csv", filename=PERFORMANCE_CSV_FILE)


@app.get("/performance/json", dependencies=[Depends(require_admin)])
async def export_performance_json():
    if not os.path.exists(PERFORMANCE_JSON_FILE):
        write_performance_json([])
    return FileResponse(PERFORMANCE_JSON_FILE, media_type="application/json", filename=PERFORMANCE_JSON_FILE)


def build_closed_trades_from_rows(rows):
    open_positions: Dict[str, Dict[str, Any]] = {}
    closed_trades = []

    for row in rows:
        action = row["action"]
        sym = row["symbol"]
        tf = row["timeframe"]
        src = row["source"]
        key = f"{src}:{sym}:{tf}"

        try:
            price = float(row["price"])
        except Exception:
            continue

        received_at = row["received_at"]
        alert_time = row["alert_timestamp"]

        if action in ["LONG", "LONG_ADD"]:
            if key not in open_positions:
                open_positions[key] = {
                    "source": src,
                    "symbol": sym,
                    "timeframe": tf,
                    "side": "LONG",
                    "contracts": 0,
                    "total_entry_value": 0.0,
                    "entry_time": received_at,
                    "entry_alert_time": alert_time,
                }

            elif open_positions[key]["side"] != "LONG":
                pos = open_positions.pop(key)
                contracts = pos["contracts"]
                avg_entry = pos["total_entry_value"] / contracts
                mult = get_multiplier(sym)
                pnl = (avg_entry - price) * contracts * mult

                closed_trades.append({
                    "source": pos["source"],
                    "symbol": sym,
                    "timeframe": tf,
                    "side": "SHORT",
                    "contracts": contracts,
                    "entry_price": round(avg_entry, 4),
                    "exit_price": price,
                    "points": round(avg_entry - price, 4),
                    "multiplier": mult,
                    "pnl": round(pnl, 2),
                    "entry_time": pos["entry_time"],
                    "exit_time": received_at,
                    "entry_alert_time": pos["entry_alert_time"],
                    "exit_alert_time": alert_time,
                })

                open_positions[key] = {
                    "source": src,
                    "symbol": sym,
                    "timeframe": tf,
                    "side": "LONG",
                    "contracts": 0,
                    "total_entry_value": 0.0,
                    "entry_time": received_at,
                    "entry_alert_time": alert_time,
                }

            open_positions[key]["contracts"] += 1
            open_positions[key]["total_entry_value"] += price

        elif action in ["SHORT", "SHORT_ADD"]:
            if key not in open_positions:
                open_positions[key] = {
                    "source": src,
                    "symbol": sym,
                    "timeframe": tf,
                    "side": "SHORT",
                    "contracts": 0,
                    "total_entry_value": 0.0,
                    "entry_time": received_at,
                    "entry_alert_time": alert_time,
                }

            elif open_positions[key]["side"] != "SHORT":
                pos = open_positions.pop(key)
                contracts = pos["contracts"]
                avg_entry = pos["total_entry_value"] / contracts
                mult = get_multiplier(sym)
                pnl = (price - avg_entry) * contracts * mult

                closed_trades.append({
                    "source": pos["source"],
                    "symbol": sym,
                    "timeframe": tf,
                    "side": "LONG",
                    "contracts": contracts,
                    "entry_price": round(avg_entry, 4),
                    "exit_price": price,
                    "points": round(price - avg_entry, 4),
                    "multiplier": mult,
                    "pnl": round(pnl, 2),
                    "entry_time": pos["entry_time"],
                    "exit_time": received_at,
                    "entry_alert_time": pos["entry_alert_time"],
                    "exit_alert_time": alert_time,
                })

                open_positions[key] = {
                    "source": src,
                    "symbol": sym,
                    "timeframe": tf,
                    "side": "SHORT",
                    "contracts": 0,
                    "total_entry_value": 0.0,
                    "entry_time": received_at,
                    "entry_alert_time": alert_time,
                }

            open_positions[key]["contracts"] += 1
            open_positions[key]["total_entry_value"] += price

        elif action == "CLOSE_ALL":
            if key in open_positions:
                pos = open_positions.pop(key)
                contracts = pos["contracts"]
                avg_entry = pos["total_entry_value"] / contracts
                side = pos["side"]
                mult = get_multiplier(sym)

                points = price - avg_entry if side == "LONG" else avg_entry - price
                pnl = points * contracts * mult

                closed_trades.append({
                    "source": pos["source"],
                    "symbol": sym,
                    "timeframe": tf,
                    "side": side,
                    "contracts": contracts,
                    "entry_price": round(avg_entry, 4),
                    "exit_price": price,
                    "points": round(points, 4),
                    "multiplier": mult,
                    "pnl": round(pnl, 2),
                    "entry_time": pos["entry_time"],
                    "exit_time": received_at,
                    "entry_alert_time": pos["entry_alert_time"],
                    "exit_alert_time": alert_time,
                })

    return closed_trades, list(open_positions.values())


@app.get("/performance", dependencies=[Depends(require_admin)])
async def get_performance(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 100,
):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM signals WHERE 1=1"
        params = []

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        if timeframe:
            query += " AND timeframe = ?"
            params.append(timeframe)

        if source:
            query += " AND source = ?"
            params.append(source)

        query += " ORDER BY received_at ASC"

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        closed_trades, open_positions_list = build_closed_trades_from_rows(rows)

        pnl_values = [t["pnl"] for t in closed_trades]
        wins = [p for p in pnl_values if p > 0]
        losses = [p for p in pnl_values if p < 0]

        closed_count = len(closed_trades)
        gross_profit = sum(wins)
        gross_loss = sum(losses)
        net_profit = gross_profit + gross_loss
        win_rate = (len(wins) / closed_count * 100) if closed_count > 0 else 0
        profit_factor = (gross_profit / abs(gross_loss)) if gross_loss != 0 else None

        by_symbol = {}

        for trade in closed_trades:
            sym = trade["symbol"]
            if sym not in by_symbol:
                by_symbol[sym] = {"closed_trades": 0, "net_profit": 0, "wins": 0}

            by_symbol[sym]["closed_trades"] += 1
            by_symbol[sym]["net_profit"] += trade["pnl"]

            if trade["pnl"] > 0:
                by_symbol[sym]["wins"] += 1

        for sym, data in by_symbol.items():
            data["win_rate"] = (data["wins"] / data["closed_trades"] * 100) if data["closed_trades"] > 0 else 0
            del data["wins"]

        write_performance_csv(closed_trades)
        write_performance_json(closed_trades)

        return {
            "status": "ok",
            "filters": {
                "symbol": symbol,
                "timeframe": timeframe,
                "source": source,
            },
            "summary": {
                "closed_trades": closed_count,
                "open_trades": len(open_positions_list),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(win_rate, 2),
                "gross_profit": round(gross_profit, 2),
                "gross_loss": round(gross_loss, 2),
                "net_profit": round(net_profit, 2),
                "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
                "best_trade": round(max(pnl_values), 2) if pnl_values else 0,
                "worst_trade": round(min(pnl_values), 2) if pnl_values else 0,
            },
            "by_symbol": by_symbol,
            "open_positions": open_positions_list,
            "closed_trades_list": closed_trades[-limit:],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Performance error: {str(e)}")


def parse_strategy_tester_csv(csv_text: str, trade_date: Optional[str] = None):
    reader = csv.DictReader(io.StringIO(csv_text))
    grouped = {}

    for row in reader:
        trade_num = row.get("Trade number")
        if not trade_num:
            continue

        dt = row.get("Date and time", "")
        if trade_date and not dt.startswith(trade_date):
            continue

        grouped.setdefault(trade_num, []).append(row)

    trades = []

    for trade_num, rows in grouped.items():
        entry = None
        exit_ = None

        for row in rows:
            row_type = row.get("Type", "").lower()

            if "entry" in row_type:
                entry = row
            elif "exit" in row_type:
                exit_ = row

        if not entry or not exit_:
            continue

        entry_type = entry.get("Type", "").lower()
        side = "LONG" if "long" in entry_type else "SHORT"

        trades.append({
            "strategy_trade_number": int(trade_num),
            "side": side,
            "entry_time": entry.get("Date and time"),
            "exit_time": exit_.get("Date and time"),
            "entry_price": float(entry.get("Price USD")),
            "exit_price": float(exit_.get("Price USD")),
            "contracts": int(float(entry.get("Size (qty)", 1))),
            "strategy_pnl": float(exit_.get("Net PnL USD")),
            "exit_signal": exit_.get("Signal"),
        })

    trades.sort(key=lambda x: x["strategy_trade_number"])
    return trades


def build_bot_closed_trades_for_reconciliation(
    source: str = "Greedy Futures Strategy",
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    trade_date: Optional[str] = None,
):
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = "SELECT * FROM signals WHERE source = ?"
    params = [source]

    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)

    if timeframe:
        query += " AND timeframe = ?"
        params.append(timeframe)

    if trade_date:
        query += " AND alert_timestamp LIKE ?"
        params.append(f"{trade_date}%")

    query += " ORDER BY received_at ASC"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    closed_trades, _ = build_closed_trades_from_rows(rows)
    return closed_trades


@app.post("/trade_reconciliation", dependencies=[Depends(require_admin)])
async def trade_reconciliation(
    file: UploadFile = File(...),
    trade_date: Optional[str] = None,
    symbol: Optional[str] = "MNQ1!",
    timeframe: Optional[str] = "15",
    source: Optional[str] = "Greedy Futures Strategy",
):
    try:
        content = await file.read()
        csv_text = content.decode("utf-8-sig")

        strategy_trades = parse_strategy_tester_csv(csv_text, trade_date=trade_date)

        bot_trades = build_bot_closed_trades_for_reconciliation(
            source=source,
            symbol=symbol,
            timeframe=timeframe,
            trade_date=trade_date,
        )

        comparisons = []
        max_len = max(len(strategy_trades), len(bot_trades))

        for idx in range(max_len):
            strategy_trade = strategy_trades[idx] if idx < len(strategy_trades) else None
            bot_trade = bot_trades[idx] if idx < len(bot_trades) else None

            if strategy_trade and bot_trade:
                side_match = strategy_trade["side"] == bot_trade["side"]
                entry_match = abs(strategy_trade["entry_price"] - bot_trade["entry_price"]) <= 0.25
                exit_match = abs(strategy_trade["exit_price"] - bot_trade["exit_price"]) <= 0.25
                pnl_match = abs(strategy_trade["strategy_pnl"] - bot_trade["pnl"]) <= 2.50

                matched = side_match and entry_match and exit_match and pnl_match

                comparisons.append({
                    "index": idx + 1,
                    "status": "MATCH" if matched else "MISMATCH",
                    "checks": {
                        "side_match": side_match,
                        "entry_price_match": entry_match,
                        "exit_price_match": exit_match,
                        "pnl_match": pnl_match,
                    },
                    "strategy": strategy_trade,
                    "bot": bot_trade,
                    "difference": {
                        "entry_price_diff": round(bot_trade["entry_price"] - strategy_trade["entry_price"], 4),
                        "exit_price_diff": round(bot_trade["exit_price"] - strategy_trade["exit_price"], 4),
                        "pnl_diff": round(bot_trade["pnl"] - strategy_trade["strategy_pnl"], 2),
                    }
                })

            elif strategy_trade and not bot_trade:
                comparisons.append({
                    "index": idx + 1,
                    "status": "MISSING_IN_BOT",
                    "strategy": strategy_trade,
                    "bot": None,
                })

            elif bot_trade and not strategy_trade:
                comparisons.append({
                    "index": idx + 1,
                    "status": "EXTRA_IN_BOT",
                    "strategy": None,
                    "bot": bot_trade,
                })

        matched_count = sum(1 for c in comparisons if c["status"] == "MATCH")
        mismatch_count = sum(1 for c in comparisons if c["status"] != "MATCH")

        return {
            "status": "ok",
            "filters": {
                "trade_date": trade_date,
                "symbol": symbol,
                "timeframe": timeframe,
                "source": source,
            },
            "summary": {
                "strategy_trades": len(strategy_trades),
                "bot_trades": len(bot_trades),
                "matched": matched_count,
                "mismatched_or_missing": mismatch_count,
            },
            "comparisons": comparisons,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reconciliation error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
