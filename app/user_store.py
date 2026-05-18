"""User store — JSON-backed user records with bcrypt passwords."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

import bcrypt


_DEFAULT_USER_DEFAULTS: dict = {
    "email_to": "",
    "ntfy_topic": "",
    "poll_interval_seconds": 300,
}


@dataclass(frozen=True)
class User:
    """Public-facing user record (no password hash)."""
    username: str
    role: str
    created_at: str
    defaults: dict = field(default_factory=dict)


class UserStore:
    """JSON-backed user store with atomic writes and bcrypt passwords."""

    def __init__(
        self,
        data_dir: str,
        *,
        bootstrap_username: str | None = None,
        bootstrap_password: str | None = None,
        monitors_path: str | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._path = os.path.join(data_dir, "users.json")
        self._monitors_path = monitors_path or os.path.join(data_dir, "monitors.json")
        os.makedirs(data_dir, exist_ok=True)
        if not os.path.exists(self._path):
            self._write({"users": []})
        # Bootstrap + migration happen here (Tasks 4 and 8 wire them in).

    # ── I/O ───────────────────────────────────────────────────────────────────

    def _read(self) -> dict:
        with open(self._path, encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        dir_ = os.path.dirname(self._path)
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_username(username: str) -> str:
        return (username or "").strip().lower()

    @staticmethod
    def _to_public(record: dict) -> User:
        return User(
            username=record["username"],
            role=record["role"],
            created_at=record.get("created_at", ""),
            defaults=dict(record.get("defaults", {})),
        )

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def add(self, *, username: str, password: str, role: str) -> User:
        """Create a new user. Raises ValueError on duplicate or invalid input."""
        uname = self._normalize_username(username)
        if not uname:
            raise ValueError("username is required")
        if role not in ("admin", "user"):
            raise ValueError(f"role must be 'admin' or 'user', got {role!r}")
        if not password:
            raise ValueError("password is required")
        data = self._read()
        if any(u["username"] == uname for u in data["users"]):
            raise ValueError(f"user {uname!r} already exists")
        record = {
            "username": uname,
            "role": role,
            "password_hash": bcrypt.hashpw(
                password.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "defaults": dict(_DEFAULT_USER_DEFAULTS),
        }
        data["users"].append(record)
        self._write(data)
        return self._to_public(record)

    def get(self, username: str) -> User | None:
        uname = self._normalize_username(username)
        for u in self._read()["users"]:
            if u["username"] == uname:
                return self._to_public(u)
        return None
