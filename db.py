"""SQLite storage for the File-to-Link bot.

Tables:
  users    - everyone who ever talked to the bot
  uploads  - every successful upload (used for stats, last link, duplicates)
  settings - key/value store (maintenance mode, custom welcome message)
  errors   - short error log for the admin panel

The database file lives next to the bot by default. Override with BOT_DB_PATH.
NOTE: on Render's free plan this file can disappear on redeploy/restart,
so statistics may reset there. On a VPS it persists normally.
"""

import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("BOT_DB_PATH", "bot.db")
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                joined_at  INTEGER,
                last_seen  INTEGER
            );
            CREATE TABLE IF NOT EXISTS uploads (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER,
                file_unique_id TEXT,
                file_name      TEXT,
                size           INTEGER,
                link           TEXT,
                created_at     INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS errors (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                category   TEXT,
                message    TEXT,
                created_at INTEGER
            );
            """
        )


# ---------------------------------------------------------------- users

def upsert_user(user_id, username, first_name):
    now = int(time.time())
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, joined_at, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen
            """,
            (user_id, username, first_name, now, now),
        )


def all_user_ids():
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
    return [r["user_id"] for r in rows]


def total_users():
    with _lock, _conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


# ---------------------------------------------------------------- uploads

def add_upload(user_id, file_unique_id, file_name, size, link):
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO uploads (user_id, file_unique_id, file_name, size, link, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, file_unique_id, file_name, size, link, int(time.time())),
        )


def find_by_unique_id(user_id, file_unique_id):
    """Return the previous upload row for this exact file from this user."""
    if not file_unique_id:
        return None
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM uploads WHERE user_id=? AND file_unique_id=?"
            " ORDER BY id DESC LIMIT 1",
            (user_id, file_unique_id),
        ).fetchone()


def last_upload(user_id):
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM uploads WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()


def user_stats(user_id):
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size),0) AS s FROM uploads WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return row["c"], row["s"]


def global_stats():
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size),0) AS s FROM uploads"
        ).fetchone()
    return total_users(), row["c"], row["s"]


# ---------------------------------------------------------------- settings

def get_setting(key, default=None):
    with _lock, _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------- errors

def log_error(user_id, category, message):
    try:
        with _lock, _conn() as conn:
            conn.execute(
                "INSERT INTO errors (user_id, category, message, created_at)"
                " VALUES (?, ?, ?, ?)",
                (user_id, category, str(message)[:500], int(time.time())),
            )
    except Exception:
        pass  # never let error-logging crash the bot


def last_errors(limit=10):
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
