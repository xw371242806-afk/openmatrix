"""H4 server-side API key management.

Server-side API keys are created, listed, and revoked via MCP tools (and later
REST). The full secret is returned ONCE at creation; only its SHA-256 hash and
a short prefix are persisted, so the plaintext can never be retrieved after
creation. Management tools are gated behind ``OPENMATRIX_MASTER_KEY`` (a
server-side admin secret); ``apikey_verify`` (for authenticating callers) is
not gated.

Storage mirrors the other single-writer stores: one async aiosqlite
connection, idempotent schema.

Schema (list/report oriented - never grid/cell, patent guardrail):
- ``api_keys``: id, key_hash, prefix, name, created_at, revoked
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from core.access_auth import get_master_key

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9 _-]{1,64}$")

KEY_PREFIX_LEN = 8
KEY_SECRET_LEN = 32  # bytes -> ~43 base64 chars


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def hash_key(secret: str) -> str:
    """SHA-256 digest of an API key secret (constant-time compare on verify)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_key_secret() -> str:
    """Generate a URL-safe API key secret with an identifying prefix."""
    raw = secrets.token_urlsafe(KEY_SECRET_LEN)
    return f"om_{raw}"


def prefix_of(secret: str) -> str:
    return secret[:KEY_PREFIX_LEN]


def keys_db_path(db_path: Path | str | None = None) -> Path | str:
    if db_path is not None:
        p = Path(db_path)
        if p.suffix:
            return p
        return p / "api_keys.db"
    env = os.environ.get("OPENMATRIX_KEYS_DB")
    if env:
        return Path(env)
    env_root = os.environ.get("OPENMATRIX_DATA_DIR") or os.environ.get("OPENMATRIX_LIBRARY_ROOT")
    if env_root:
        return Path(env_root) / "api_keys.db"
    return Path(".openmatrix") / "api_keys.db"


def master_key_configured() -> bool:
    return bool(get_master_key())


def require_master_key(candidate: str | None) -> bool:
    """Constant-time comparison against OPENMATRIX_MASTER_KEY.

    Returns False when the master key is unset, the candidate is missing, or
    the candidate does not match.
    """
    expected = get_master_key()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(expected, candidate)


class ApiKeyStore:
    """Async single-writer API key store (H4)."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            key_hash TEXT NOT NULL UNIQUE,
            prefix TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            created_at TEXT NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        )
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = keys_db_path(db_path)
        self._conn: Any | None = None
        self._open_lock: asyncio.Lock | None = None

    async def _ensure_open(self) -> Any:
        if self._conn is not None:
            return self._conn
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        async with self._open_lock:
            if self._conn is None:
                import aiosqlite

                db = Path(self._db_path)
                db.parent.mkdir(parents=True, exist_ok=True)
                self._conn = await aiosqlite.connect(self._db_path)
                self._conn.row_factory = aiosqlite.Row
                await self._conn.executescript(self.SCHEMA)
                # migrate legacy DBs that predate the role column
                cols = await self._conn.execute_fetchall("PRAGMA table_info(api_keys)")
                if cols and all(c[1] != "role" for c in cols):
                    await self._conn.execute(
                        "ALTER TABLE api_keys ADD COLUMN role TEXT NOT NULL DEFAULT 'member'"
                    )
                await self._conn.commit()
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def create_key(self, name: str, role: str = "member") -> dict:
        """Create a key; returns the secret ONCE with its metadata.

        ``role`` is one of admin / member / readonly (P7-1 RBAC). The role is
        checked by the API auth middleware to gate sensitive endpoints.
        """
        if not _SAFE_NAME_RE.match(name):
            raise ValueError("invalid name: use 1-64 chars of [a-zA-Z0-9 _-]")
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: use one of {', '.join(VALID_ROLES)}")
        conn = await self._ensure_open()
        secret = generate_key_secret()
        key_id = "k-" + secrets.token_hex(8)
        key_hash = hash_key(secret)
        now = _now()
        await conn.execute(
            "INSERT INTO api_keys (id, key_hash, prefix, name, role, created_at, revoked)"
            " VALUES (?, ?, ?, ?, ?, ?, 0)",
            (key_id, key_hash, prefix_of(secret), name.strip(), role, now),
        )
        await conn.commit()
        return {
            "id": key_id,
            "name": name.strip(),
            "role": role,
            "prefix": prefix_of(secret),
            "created_at": now,
            "secret": secret,  # returned exactly once
        }

    async def list_keys(self) -> list[dict]:
        conn = await self._ensure_open()
        cur = await conn.execute(
            "SELECT id, prefix, name, role, created_at, revoked FROM api_keys ORDER BY created_at"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def revoke_key(self, key_id: str) -> bool:
        conn = await self._ensure_open()
        cur = await conn.execute(
            "UPDATE api_keys SET revoked = 1 WHERE id = ? AND revoked = 0", (key_id,)
        )
        await conn.commit()
        return cur.rowcount > 0

    async def verify_key(self, secret: str) -> dict | None:
        """Verify a key secret; returns its metadata or None if invalid/revoked."""
        if not secret:
            return None
        conn = await self._ensure_open()
        key_hash = hash_key(secret)
        cur = await conn.execute(
            "SELECT id, prefix, name, role, created_at, revoked FROM api_keys WHERE key_hash = ?",
            (key_hash,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        rec = dict(row)
        if rec["revoked"]:
            return None
        return rec


VALID_ROLES = ("admin", "member", "readonly")


_default_store: ApiKeyStore | None = None


def default_key_store(db_path: Path | str | None = None) -> ApiKeyStore:
    """Return the module-level default ApiKeyStore (created lazily)."""
    global _default_store
    if _default_store is None:
        _default_store = ApiKeyStore(db_path=db_path)
    return _default_store
