#!/usr/bin/env python3
"""Apply a reviewed, fail-closed patch to ifeanchi/tradingViewWebhookBot main.py.

Run from the cloned project root: python apply_qaisar_security.py
Aborts on unexpected upstream structure instead of silently patching incorrectly.
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys


def one(src: str, old: str, new: str, label: str) -> str:
    count = src.count(old)
    if count != 1:
        raise RuntimeError(f'Upstream mismatch in {label} (expected one anchor, found {count}). No changes saved.')
    return src.replace(old, new, 1)


def patch_main(src: str) -> str:
    if 'Qaisar security hardening v1' in src:
        raise RuntimeError('Patch already applied. No changes saved.')

    src = one(src,
        'from fastapi.responses import JSONResponse, FileResponse\n',
        'from fastapi.responses import JSONResponse, FileResponse\n'
        'from qaisar_security import (read_limited_body, redact_sensitive, secure_equal, '\
        'safe_json_text, RECONCILIATION_MAX_BYTES)  # Qaisar security hardening v1\n',
        'security imports')

    src = one(src,
        'if not WEBHOOK_SECRET:\n    raise ValueError("WEBHOOK_SECRET environment variable is required")\n',
        'if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 32:\n'
        '    raise ValueError("WEBHOOK_SECRET must be at least 32 characters")\n'
        'ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "")\n'
        'if len(ADMIN_API_TOKEN) < 32 or secure_equal(ADMIN_API_TOKEN, WEBHOOK_SECRET):\n'
        '    raise ValueError("ADMIN_API_TOKEN must be distinct and at least 32 characters")\n',
        'strong independent secrets')

    src = one(src,
        '\n\nclass WebhookPayload(BaseModel):',
        '\n\n@app.middleware("http")\n'
        'async def protect_private_routes(request: Request, call_next):\n'
        '    # Public access is limited to health and TradingView webhook routes.\n'
        '    if request.url.path not in ("/health", "/webhook"):\n'
        '        if not secure_equal(request.headers.get("x-admin-token"), ADMIN_API_TOKEN):\n'
        '            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})\n'
        '    return await call_next(request)\n'
        '\n\nclass WebhookPayload(BaseModel):',
        'private endpoint middleware')

    src = one(src,
        '        raw_body = await request.body()\n        original_raw_payload = raw_body.decode("utf-8")\n',
        '        raw_body = await read_limited_body(request)\n'
        '        try:\n'
        '            original_raw_payload = raw_body.decode("utf-8")\n'
        '        except UnicodeDecodeError:\n'
        '            raise HTTPException(status_code=400, detail="Invalid UTF-8 payload")\n',
        'bounded payload decoding')

    src = one(src,
        '        if "secret" not in data:\n',
        '        if not isinstance(data, dict):\n'
        '            raise HTTPException(status_code=400, detail="Expected a JSON object")\n'
        '        if "secret" not in data:\n',
        'JSON type verification')

    src = one(src,
        '        if data.get("secret") != WEBHOOK_SECRET:\n',
        '        if not secure_equal(data.get("secret"), WEBHOOK_SECRET):\n',
        'constant-time webhook authentication')

    src = one(src,
        '            normalized_data = convert_strategy_to_bot_payload(data)\n',
        '            try:\n'
        '                normalized_data = convert_strategy_to_bot_payload(data)\n'
        '            except (ValueError, TypeError, OverflowError):\n'
        '                raise HTTPException(status_code=400, detail="Invalid strategy schema")\n',
        'strategy validation status')

    src = one(src,
        '        try:\n            payload = WebhookPayload(**normalized_data)\n',
        '        # Never persist raw webhook keys in SQLite or CSV.\n'
        '        raw_payload_to_store = safe_json_text(json.loads(raw_payload_to_store))\n'
        '        try:\n            payload = WebhookPayload(**normalized_data)\n',
        'secret redaction')

    src = one(src,
        '            raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")\n',
        '            raise HTTPException(status_code=400, detail="Invalid alert schema")\n',
        'validation error content')

    for label in ('Internal error', 'Stats error', 'Performance error', 'Reconciliation error'):
        old = f'        raise HTTPException(status_code=500, detail=f"{label}: {{str(e)}}")'
        src = one(src, old, '        raise HTTPException(status_code=500, detail="Internal server error")', 'internal error masking: '+label)

    src = one(src,
        '        content = await file.read()\n        csv_text = content.decode("utf-8-sig")\n',
        '        content = await file.read(RECONCILIATION_MAX_BYTES + 1)\n'
        '        if len(content) > RECONCILIATION_MAX_BYTES:\n'
        '            raise HTTPException(status_code=413, detail="Uploaded CSV exceeds 1 MiB")\n'
        '        try:\n'
        '            csv_text = content.decode("utf-8-sig")\n'
        '        except UnicodeDecodeError:\n'
        '            raise HTTPException(status_code=400, detail="Invalid CSV encoding")\n',
        'bounded CSV upload')

    # Keep intentional 413/400 errors from being converted to generic 500.
    src = one(src,
        '        except UnicodeDecodeError:\n            raise HTTPException(status_code=400, detail="Invalid CSV encoding")\n\n        strategy_trades',
        '        except UnicodeDecodeError:\n            raise HTTPException(status_code=400, detail="Invalid CSV encoding")\n\n        strategy_trades',
        'CSV validation anchor')  # explicit structural check

    src = one(src,
        '    except Exception as e:\n        raise HTTPException(status_code=500, detail="Internal server error")\n\n\nif __name__ == "__main__":',
        '    except HTTPException:\n        raise\n'
        '    except Exception:\n        raise HTTPException(status_code=500, detail="Internal server error")\n\n\nif __name__ == "__main__":',
        'reconciliation HTTP status preservation')

    # Fail closed on coding changes to expected private endpoints.
    expected = ['/signals', '/stats', '/performance/export', '/performance/json', '/performance', '/trade_reconciliation']
    for endpoint in expected:
        if f'"{endpoint}"' not in src:
            raise RuntimeError(f'Expected endpoint missing: {endpoint}')

    ast.parse(src)
    return src


def main() -> None:
    main_path = Path('main.py')
    support = Path(__file__).resolve().with_name('qaisar_security.py')
    if not main_path.is_file():
        sys.exit('Run inside the downloaded TradingView Webhook Bot folder (main.py is missing).')
    if not support.is_file():
        sys.exit('Missing qaisar_security.py next to patch script.')
    target_support = Path('qaisar_security.py')
    # Accept a bundled helper placed next to the patcher inside the repository.
    # Refuse to overwrite another version of the helper silently.
    if target_support.exists() and target_support.read_text(encoding='utf-8') != support.read_text(encoding='utf-8'):
        sys.exit('Existing qaisar_security.py differs from bundled helper; no files changed.')
    old = main_path.read_text(encoding='utf-8')
    try:
        new = patch_main(old)
    except Exception as exc:
        sys.exit(f'No changes made: {exc}')
    requirements = Path('requirements.txt')
    if not requirements.is_file():
        sys.exit('No changes made: requirements.txt missing.')
    req = requirements.read_text(encoding='utf-8')
    if 'python-multipart' not in req:
        req = req.rstrip() + '\npython-multipart>=0.0.20,<1\n'
    env_example = Path('.env.example')
    if not env_example.is_file():
        sys.exit('No changes made: .env.example missing.')
    new_env_example = (
        '# Generate two DIFFERENT random secrets (for example, using secrets.token_urlsafe(32)).\n'
        '# These fields are intentionally blank, and the application refuses to start until configured.\n'
        'WEBHOOK_SECRET=\n'
        'ADMIN_API_TOKEN=\n'
    )
    # All expected source checks and syntax checks run before any file write.
    main_path.write_text(new, encoding='utf-8')
    if not target_support.exists():
        target_support.write_text(support.read_text(encoding='utf-8'), encoding='utf-8')
    requirements.write_text(req, encoding='utf-8')
    env_example.write_text(new_env_example, encoding='utf-8')
    print('Security patch applied locally. No file uploaded to GitHub.')
    print('Set WEBHOOK_SECRET and ADMIN_API_TOKEN independently (32+ characters each).')
    print('Run tests; rotate any previously used webhook secret and scrub old logs.')

if __name__ == '__main__':
    main()
