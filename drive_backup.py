"""Google Drive snapshot storage for the SQLite signal archive.

The OAuth client and refresh token are supplied only through environment variables.
The app uses the drive.file scope and creates a visible folder in My Drive.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx


FOLDER_NAME = "Qaisar Radar Backups"
BACKUP_NAME = "signals.sqlite3"
MAX_BACKUP_BYTES = 20 * 1024 * 1024
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_API = "https://oauth2.googleapis.com/token"
REDACT_KEYS = {"secret", "webhook_secret", "admin_token", "admin_api_token", "api_key", "authorization", "access_token", "refresh_token"}


def redact_payload(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in REDACT_KEYS else redact_payload(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def redact_json_text(raw: str) -> str:
    try:
        return json.dumps(redact_payload(json.loads(raw)), ensure_ascii=False)
    except (ValueError, TypeError):
        # Never export unrecognized historical raw text that might contain a key.
        return json.dumps({"legacy_payload": "[REDACTED]"})


class DriveBackup:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 client: httpx.Client | None = None):
        if not all((client_id, client_secret, refresh_token)):
            raise ValueError("Google Drive OAuth configuration is incomplete")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.client = client or httpx.Client(timeout=25)
        self._access_token = None
        self._expires_at = 0.0
        self._folder_id = None
        self._file_id = None
        self.lock = threading.RLock()

    @classmethod
    def from_environment(cls):
        names = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
        values = [os.getenv(name) for name in names]
        if not any(values):
            return None
        return cls(*values)

    def _token(self):
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        response = self.client.post(TOKEN_API, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        })
        response.raise_for_status()
        data = response.json()
        self._access_token = data["access_token"]
        self._expires_at = time.monotonic() + max(0, int(data.get("expires_in", 3600)) - 120)
        return self._access_token

    def _request(self, method, url, **kwargs):
        headers = kwargs.pop("headers", {})
        response = self.client.request(method, url, headers={
            **headers, "Authorization": f"Bearer {self._token()}"}, **kwargs)
        response.raise_for_status()
        return response

    def _lookup(self, name, mime_type, parent=None):
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped}' and mimeType = '{mime_type}' and trashed = false"
        if parent:
            query += f" and '{parent}' in parents"
        response = self._request("GET", DRIVE_API, params={
            "q": query, "spaces": "drive", "fields": "files(id,name),nextPageToken", "pageSize": 100,
        }).json()
        files = response.get("files", [])
        if len(files) > 1:
            raise RuntimeError("Multiple matching Drive backup files; refusing to choose one")
        return files[0]["id"] if files else None

    def _folder(self):
        if self._folder_id is None:
            self._folder_id = self._lookup(FOLDER_NAME, "application/vnd.google-apps.folder")
            if self._folder_id is None:
                self._folder_id = self._request("POST", DRIVE_API, params={"fields": "id"},
                    json={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"}).json()["id"]
        return self._folder_id

    def _file(self):
        if self._file_id is None:
            self._file_id = self._lookup(BACKUP_NAME, "application/x-sqlite3", self._folder())
        return self._file_id

    def restore(self, destination: str) -> bool:
        """Restore before opening the database; fail startup if Drive is unavailable."""
        with self.lock:
            file_id = self._file()
            if not file_id:
                return False
            response = self._request("GET", f"{DRIVE_API}/{file_id}", params={"alt": "media"})
            if len(response.content) > MAX_BACKUP_BYTES:
                raise RuntimeError("Drive backup exceeds configured size limit")
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="restore-", suffix=".db", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(response.content)
                with closing(sqlite3.connect(tmp)) as con:
                    if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("Drive backup failed SQLite integrity check")
                    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signals'").fetchone():
                        raise RuntimeError("Drive backup does not contain the signals table")
                    remote_max_id = con.execute("SELECT COALESCE(MAX(id), 0) FROM signals").fetchone()[0]
                if path.exists():
                    with closing(sqlite3.connect(path)) as local:
                        local_max_id = local.execute("SELECT COALESCE(MAX(id), 0) FROM signals").fetchone()[0]
                    if local_max_id > remote_max_id:
                        raise RuntimeError("Local database is newer than Drive backup; refusing to overwrite it")
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return True

    def _snapshot(self, source: str) -> bytes:
        fd, tmp = tempfile.mkstemp(prefix="signals-backup-", suffix=".db")
        os.close(fd)
        try:
            with closing(sqlite3.connect(source)) as live, closing(sqlite3.connect(tmp)) as copy:
                live.backup(copy)
                rows = copy.execute("SELECT id, raw_payload FROM signals").fetchall()
                with copy:
                    copy.executemany("UPDATE signals SET raw_payload=? WHERE id=?", [
                        (redact_json_text(raw), row_id) for row_id, raw in rows
                    ])
                # Rebuild pages so deleted historical credentials cannot remain
                # in unused SQLite bytes uploaded to Drive.
                copy.execute("VACUUM")
            data = Path(tmp).read_bytes()
            if len(data) > MAX_BACKUP_BYTES:
                raise RuntimeError("SQLite archive exceeds configured Drive backup size limit")
            return data
        finally:
            os.unlink(tmp)

    def save(self, source: str) -> None:
        with self.lock:
            data = self._snapshot(source)
            file_id = self._file()
            if file_id:
                self._request("PATCH", f"{UPLOAD_API}/{file_id}", params={"uploadType": "media"},
                              content=data, headers={"Content-Type": "application/x-sqlite3"})
            else:
                metadata = json.dumps({"name": BACKUP_NAME,
                                       "mimeType": "application/x-sqlite3",
                                       "parents": [self._folder()]}).encode()
                boundary = "qaisar_sqlite_backup_boundary"
                body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                        + metadata + f"\r\n--{boundary}\r\nContent-Type: application/x-sqlite3\r\n\r\n".encode()
                        + data + f"\r\n--{boundary}--\r\n".encode())
                result = self._request("POST", UPLOAD_API, params={"uploadType": "multipart", "fields": "id"},
                    content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
                self._file_id = result.json()["id"]
