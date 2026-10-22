# TradingView Webhook Bot

A Python FastAPI webhook server that receives TradingView indicator and strategy alerts, normalizes them, and logs them to SQLite + CSV for performance analysis.

## 🎯 Purpose

This bot receives trading signals from TradingView and:
- ✅ Validates webhook authentication
- ✅ Accepts both indicator alerts and strategy alerts
- ✅ Normalizes strategy alerts into bot actions
- ✅ Logs all signals to SQLite database
- ✅ Creates CSV backup for easy analysis
- ✅ Prevents duplicate signals (10-second window)
- ✅ Provides API to query logged signals and performance

## ⚠️ Important: Logging Only

**This bot does NOT place trades.** It only logs signals for:
- Backtesting strategy performance
- Manual review before execution
- Building trading history
- Future broker integration (coming later)

## 📁 Project Structure

```
tradingview-webhook-bot/
├── main.py              # FastAPI application
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variables template
├── .env                 # Your actual secrets (gitignored)
├── signals.db           # SQLite database (auto-created)
├── trade_signals.csv    # CSV backup (auto-created)
├── performance_trades.csv # Performance export (auto-created)
├── performance_trades.json # Performance export (auto-created)
└── README.md            # This file
```

## 🚀 Quick Start

### 1. Clone/Setup Project

```bash
cd tradingview-webhook-bot
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Set Environment Variables

```bash
# Copy example file
cp .env.example .env
# On Windows PowerShell use: copy .env.example .env

# Edit .env and configure both independent secrets
nano .env
```

Your `.env` file should look like:
```bash
# Generate each value separately with: python -c "import secrets; print(secrets.token_urlsafe(32))"
WEBHOOK_SECRET=<random value of at least 32 characters>
ADMIN_API_TOKEN=<different random value of at least 32 characters>
```

### 3. Run the Server

```bash
python main.py
```

Server will start on `http://localhost:8001` when run with `python main.py`.
The deployment examples below explicitly use port 8000 via Uvicorn.

### Security configuration

Deploy `main:app`. The legacy `main2.py`, `main3.py`, and `main4.py` are not
covered by these fixes and must not be exposed as alternative entry points.

Only `/health` and `/webhook` are public. All other routes, including API docs,
require the `X-Admin-Token` header. Never put the admin token in a TradingView
alert; alerts use only `WEBHOOK_SECRET`. The server refuses startup with missing,
short, or identical credentials. Update any clients that read reports accordingly.

Webhook bodies are limited to 64 KiB, and reconciliation CSV files to 1 MiB.
Use HTTPS and enforce upload limits and request rates at the reverse proxy as
well, since multipart parsing happens before the endpoint reads an uploaded file.
Stored webhook JSON redacts recognized credential keys recursively. Avoid placing
credentials in free-text fields; those fields are not detected as secrets.

If an older deployment stored real webhook secrets, rotate those secrets.
`scrub_history.py` can redact recognized keys in local `signals.db` and
`trade_signals.csv`; make a secure offline backup before running it. It does not
rewrite Git history, archived fixtures, WAL files, or backups.

Run the isolated application and security checks without a live server:

```bash
python -m pytest tests -q
```

## 🔌 API Endpoints

### 1. POST /webhook
Receive TradingView alerts.

This endpoint supports two payload formats:
- Indicator alert payload with normalized `action` values
- Strategy alert payload using `order_action`, `order_contracts`, `order_price`, and `position_size`

#### Indicator payload fields
- `secret`
- `source`
- `action` — one of: `LONG`, `SHORT`, `LONG_ADD`, `SHORT_ADD`, `CLOSE_ALL`
- `symbol`
- `price`
- `timeframe`
- `exchange`
- `timestamp`

#### Strategy payload fields
- `secret`
- `source`
- `order_action` (`buy` or `sell`)
- `order_contracts`
- `order_price`
- `position_size`
- `symbol`
- `timeframe`
- `exchange`
- `timestamp`

The server automatically converts strategy payloads into normalized bot actions and logs the resulting signal.

#### Example indicator payload
```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "source": "Greedy Futures Indicator",
  "action": "LONG",
  "symbol": "MNQ1!",
  "price": "22500.25",
  "timeframe": "15",
  "exchange": "CME_MINI",
  "timestamp": "2026-06-08T09:45:00Z"
}
```

#### Example strategy payload
```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "source": "Greedy Futures Strategy",
  "order_action": "buy",
  "order_contracts": "1",
  "order_price": "22500.25",
  "position_size": "1",
  "symbol": "MNQ1!",
  "timeframe": "15",
  "exchange": "CME_MINI",
  "timestamp": "2026-06-08T09:45:00Z"
}
```

**Response (Success):**
```json
{
  "status": "success",
  "id": 1,
  "source": "Greedy Futures Strategy",
  "action": "LONG",
  "symbol": "MNQ1!",
  "price": "22500.25",
  "message": "Signal logged successfully"
}
```

**Response (Duplicate):**
```json
{
  "status": "ignored",
  "reason": "duplicate_detected",
  "action": "LONG",
  "symbol": "MNQ1!"
}
```

### 2. GET /health
Health check

**Response:**
```json
{"status": "ok"}
```

### 3. GET /signals
Get latest signals

**Response:**
```json
{
  "signals": [
    {
      "id": 1,
      "received_at": "2026-06-08T09:45:01Z",
      "source": "Greedy Futures Indicator",
      "action": "LONG",
      "symbol": "MNQ1!",
      "price": "22500.25",
      "timeframe": "15",
      "exchange": "CME_MINI",
      "alert_timestamp": "2026-06-08T09:45:00Z"
    }
  ],
  "count": 1
}
```

### 4. GET /stats
Get summary stats for logged signals

### 5. GET /performance
Reconstruct simulated trade performance from logged signals

Optional query filters:
- `symbol`
- `timeframe`
- `source`
- `limit`

### 6. GET /performance/export
Download performance CSV export

### 7. GET /performance/json
Download performance JSON export

## 🧪 Testing

### Test with curl

```bash
# Health check
curl http://localhost:8000/health

# Valid indicator webhook
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "YOUR_WEBHOOK_SECRET",
    "source": "Greedy Futures Indicator",
    "action": "LONG",
    "symbol": "MNQ1!",
    "price": "22500.25",
    "timeframe": "15",
    "exchange": "CME_MINI",
    "timestamp": "2026-06-08T09:45:00Z"
  }'

# Valid strategy webhook
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "YOUR_WEBHOOK_SECRET",
    "source": "Greedy Futures Strategy",
    "order_action": "buy",
    "order_contracts": "1",
    "order_price": "22500.25",
    "position_size": "1",
    "symbol": "MNQ1!",
    "timeframe": "15",
    "exchange": "CME_MINI",
    "timestamp": "2026-06-08T09:45:00Z"
  }'

# Get signals
curl -H "X-Admin-Token: $ADMIN_API_TOKEN" http://localhost:8000/signals
```

## 📊 TradingView Alert Configuration

### Exact TradingView alert templates

#### Indicator alert template
```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "source": "Zone Sweep Indicator",
  "action": "LONG",
  "symbol": "{{ticker}}",
  "price": "{{close}}",
  "timeframe": "{{interval}}",
  "exchange": "{{exchange}}",
  "timestamp": "{{time}}"
}
```

#### Strategy alert template
```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "source": "Greedy Futures Strategy",
  "order_action": "{{strategy.order.action}}",
  "order_contracts": "{{strategy.order.contracts}}",
  "order_price": "{{strategy.order.price}}",
  "position_size": "{{strategy.position_size}}",
  "symbol": "{{ticker}}",
  "timeframe": "{{interval}}",
  "exchange": "{{exchange}}",
  "timestamp": "{{time}}"
}
```

TradingView will send the strategy payload and the bot converts it into a normalized signal action automatically.

## 🌐 Deployment

### Option 1: VPS/Cloud Server (Recommended)

**Prerequisites:**
- Ubuntu 20.04+ server
- Python 3.9+
- Domain (optional)

**Setup:**
```bash
# On your server
git clone <your-repo>
cd tradingview-webhook-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your secret
nano .env
```

**Run with Systemd:**
Create `/etc/systemd/system/tradingview-bot.service`:
```ini
[Unit]
Description=TradingView Webhook Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/tradingview-webhook-bot
Environment="PATH=/home/ubuntu/tradingview-webhook-bot/venv/bin"
ExecStart=/home/ubuntu/tradingview-webhook-bot/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable tradingview-bot
sudo systemctl start tradingview-bot
sudo systemctl status tradingview-bot
```

**With Nginx (SSL):**
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Option 2: Railway/Render (Easy Deploy)

**Railway:**
1. Push code to GitHub
2. Connect Railway to repo
3. Add `WEBHOOK_SECRET` environment variable
4. Deploy

**Render:**
1. Create new Web Service
2. Connect GitHub repo
3. Set environment variables
4. Deploy

### Option 3: Docker

Create `Dockerfile`:
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Build and run:
```bash
docker build -t tradingview-bot .
docker run -p 8000:8000 --env-file .env tradingview-bot
```

## 🔒 Security Best Practices

1. **Keep WEBHOOK_SECRET safe:**
   - Never commit `.env` to git
   - Use strong secret (32+ chars)
   - Rotate periodically

2. **Use HTTPS in production:**
   - TradingView requires HTTPS for webhooks
   - Use Let's Encrypt free SSL

3. **Firewall:**
   - Only open port 8000 (or 443)
   - Restrict IP if possible

4. **Database:**
   - SQLite is fine for logging
   - Back up `signals.db` regularly

## 📈 Viewing Your Data

### SQLite CLI
```bash
sqlite3 signals.db

# View last 10 signals
SELECT * FROM signals ORDER BY received_at DESC LIMIT 10;

# Count signals by action
SELECT action, COUNT(*) FROM signals GROUP BY action;

# View today's signals
SELECT * FROM signals WHERE DATE(received_at) = DATE('now');
```

### CSV Analysis
Open `trade_signals.csv` in Excel, Google Sheets, or Python pandas:
```python
import pandas as pd
df = pd.read_csv('trade_signals.csv')
print(df.groupby('action').count())
```

## 🔧 Troubleshooting

**Error: "WEBHOOK_SECRET environment variable is required"**
→ Create `.env` file with your secret

**Error: "Invalid secret"**
→ Check that TradingView alert includes correct secret

**Signals not appearing**
→ Check `/health` endpoint
→ Verify TradingView webhook URL is correct
→ Check server firewall

**Duplicates not filtering**
→ Verify `DUPLICATE_WINDOW_SECONDS` (default: 10s)
→ Check that action+symbol+timeframe+price are identical

## 📝 License

MIT License - Free to use and modify

## 🚀 Roadmap

- [ ] Add broker execution (IBKR, Alpaca)
- [ ] Telegram/Discord notifications
- [ ] Web dashboard for signal analysis
- [ ] Paper trading mode
- [ ] Risk management rules

## 💬 Support

For issues or questions, check:
1. Server logs: `journalctl -u tradingview-bot -f`
2. TradingView alert logs
3. Test with curl before TradingView

---

**Built for the Zone Sweep Trading Strategy** 🎯
