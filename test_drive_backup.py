import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

# The backup's SQLite logic can be tested without installing HTTP libraries.
sys.modules.setdefault("httpx", types.SimpleNamespace(Client=object))
from drive_backup import DriveBackup, redact_json_text


class BackupTests(unittest.TestCase):
    def test_snapshot_redacts_historical_secret_and_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            source = str(Path(folder) / "signals.db")
            restored = str(Path(folder) / "restored.db")
            with sqlite3.connect(source) as conn:
                conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, raw_payload TEXT)")
                conn.execute("INSERT INTO signals VALUES (1, ?)", (json.dumps({
                    "secret": "private", "nested": {"api_key": "another-private"},
                    "symbol": "NVDA"}),))
            backup = DriveBackup("client", "client-secret", "refresh", client=object())
            snapshot = backup._snapshot(source)
            self.assertNotIn(b"private", snapshot)
            backup._file = lambda: "drive-file"
            backup._request = lambda *args, **kwargs: types.SimpleNamespace(content=snapshot)
            self.assertTrue(backup.restore(restored))
            with sqlite3.connect(restored) as conn:
                payload = json.loads(conn.execute("SELECT raw_payload FROM signals").fetchone()[0])
            self.assertEqual(payload["secret"], "[REDACTED]")
            self.assertEqual(payload["nested"]["api_key"], "[REDACTED]")
            self.assertEqual(payload["symbol"], "NVDA")

    def test_invalid_remote_file_does_not_replace_local_database(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "signals.db"
            target.write_bytes(b"original database")
            backup = DriveBackup("client", "secret", "refresh", client=object())
            backup._file = lambda: "drive-file"
            backup._request = lambda *args, **kwargs: types.SimpleNamespace(content=b"not sqlite")
            with self.assertRaises(sqlite3.DatabaseError):
                backup.restore(str(target))
            self.assertEqual(target.read_bytes(), b"original database")

    def test_non_json_legacy_payload_is_never_copied_verbatim(self):
        self.assertNotIn("credential", redact_json_text("credential in plain text"))

    def test_newer_local_database_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            source = str(Path(folder) / "source.db")
            local = str(Path(folder) / "local.db")
            for filename, identifier in ((source, 1), (local, 2)):
                with sqlite3.connect(filename) as conn:
                    conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, raw_payload TEXT)")
                    conn.execute("INSERT INTO signals VALUES (?, '{}')", (identifier,))
            backup = DriveBackup("client", "secret", "refresh", client=object())
            snapshot = backup._snapshot(source)
            backup._file = lambda: "drive-file"
            backup._request = lambda *args, **kwargs: types.SimpleNamespace(content=snapshot)
            with self.assertRaisesRegex(RuntimeError, "newer"):
                backup.restore(local)
            with sqlite3.connect(local) as conn:
                self.assertEqual(conn.execute("SELECT MAX(id) FROM signals").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
