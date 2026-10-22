#!/usr/bin/env python3
"""Redact already-saved webhook JSON in SQLite and CSV (irreversible in place).

Run only after making a secure OFFLINE backup. Rotate previously exposed secrets.
"""
import csv
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from qaisar_security import safe_json_text


def scrub(raw: str) -> str:
    try:
        return safe_json_text(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return '[REDACTED: INVALID ORIGINAL JSON]'


def main():
    db = Path('signals.db')
    if db.exists():
        with sqlite3.connect(db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute('SELECT id, raw_payload FROM signals').fetchall()
            conn.executemany('UPDATE signals SET raw_payload=? WHERE id=?',
                             [(scrub(raw), i) for i, raw in rows])
            conn.commit()
        print(f'Redacted {len(rows)} rows in {db}')
    csv_path = Path('trade_signals.csv')
    if csv_path.exists():
        with csv_path.open('r', encoding='utf-8', newline='') as fp:
            reader = csv.DictReader(fp)
            fieldnames = reader.fieldnames
            rows = list(reader)
        if not fieldnames or 'raw_payload' not in fieldnames:
            raise RuntimeError('Missing expected raw_payload header; CSV not altered')
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='', dir=csv_path.parent,
                                         prefix='.qaisar-', suffix='.csv', delete=False) as tmp:
            tmpname = tmp.name
            writer = csv.DictWriter(tmp, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                row['raw_payload'] = scrub(row['raw_payload'])
                writer.writerow(row)
        os.replace(tmpname, csv_path)
        print(f'Redacted {len(rows)} rows in {csv_path}')
    print('Rotate WEBHOOK_SECRET even after scrubbing; consider SQLite WAL/backups and old exports.')

if __name__ == '__main__':
    main()
