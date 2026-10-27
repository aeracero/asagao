"""SQLite（Railway Volume 上）とのやり取り。接続は1本だけ持つ。"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import ALARM_COLUMNS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    avatar     TEXT,
    paused     INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions(
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    guild_ids  TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sound_files(
    hash       TEXT PRIMARY KEY,
    ext        TEXT NOT NULL,
    duration   REAL NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sounds(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    hash       TEXT NOT NULL REFERENCES sound_files(hash),
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sounds_user ON sounds(user_id);
CREATE TABLE IF NOT EXISTS alarms(
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    guild_id            INTEGER,
    channel_id          INTEGER,
    weekday             INTEGER NOT NULL,
    start_min           INTEGER NOT NULL,
    end_min             INTEGER NOT NULL,
    group_id            TEXT,
    label               TEXT NOT NULL DEFAULT '',
    enabled             INTEGER NOT NULL DEFAULT 1,
    volume              INTEGER NOT NULL DEFAULT 60,
    sound_mode          TEXT NOT NULL DEFAULT 'random',
    play_count          INTEGER NOT NULL DEFAULT 3,
    play_interval       INTEGER NOT NULL DEFAULT 5,
    snooze_count        INTEGER NOT NULL DEFAULT 0,
    snooze_interval     INTEGER NOT NULL DEFAULT 5,
    snooze_leave        INTEGER NOT NULL DEFAULT 1,
    stop_mode           TEXT NOT NULL DEFAULT 'button',
    join_offset         INTEGER NOT NULL DEFAULT 15,
    require_user_in_vc  INTEGER NOT NULL DEFAULT 1,
    wait_for_user       INTEGER NOT NULL DEFAULT 0,
    leave_on_user_leave INTEGER NOT NULL DEFAULT 1,
    leave_delay         INTEGER NOT NULL DEFAULT 3,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alarms_user ON alarms(user_id);
CREATE INDEX IF NOT EXISTS idx_alarms_guild ON alarms(guild_id);
CREATE TABLE IF NOT EXISTS alarm_sounds(
    alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
    sound_id INTEGER NOT NULL REFERENCES sounds(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(alarm_id, sound_id)
);
CREATE TABLE IF NOT EXISTS alarm_overrides(
    alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
    date     TEXT NOT NULL,
    type     TEXT NOT NULL,
    PRIMARY KEY(alarm_id, date)
);
CREATE TABLE IF NOT EXISTS alarm_runs(
    alarm_id   INTEGER NOT NULL,
    date       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(alarm_id, date)
);
"""


def _now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    # --- 基本 ---------------------------------------------------------------
    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def all(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, tuple(args)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def one(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        async with self.conn.execute(sql, tuple(args)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def run(self, sql: str, args: Iterable[Any] = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(sql, tuple(args))
        await self.conn.commit()
        return cur

    async def backup(self, dest: Path) -> None:
        """稼働中でも安全にコピーを取る（SQLite の backup API）。"""
        tmp = dest.with_suffix(".tmp")
        async with aiosqlite.connect(tmp) as target:
            await self.conn.backup(target)
        tmp.replace(dest)

    # --- users / sessions ---------------------------------------------------
    async def upsert_user(self, user_id: int, username: str | None = None, avatar: str | None = None) -> None:
        await self.run(
            """INSERT INTO users(user_id, username, avatar, updated_at) VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=COALESCE(excluded.username, users.username),
                 avatar=COALESCE(excluded.avatar, users.avatar),
                 updated_at=excluded.updated_at""",
            (user_id, username, avatar, _now()),
        )

    async def get_user(self, user_id: int) -> dict | None:
        return await self.one("SELECT * FROM users WHERE user_id=?", (user_id,))

    async def set_paused(self, user_id: int, paused: bool) -> None:
        await self.upsert_user(user_id)
        await self.run("UPDATE users SET paused=? WHERE user_id=?", (int(paused), user_id))

    async def create_session(self, token_hash: str, user_id: int, guild_ids: str, expires_at: int) -> None:
        await self.run(
            "INSERT INTO sessions(token_hash, user_id, guild_ids, created_at, expires_at) VALUES(?,?,?,?,?)",
            (token_hash, user_id, guild_ids, _now(), expires_at),
        )

    async def get_session(self, token_hash: str) -> dict | None:
        return await self.one(
            "SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash, _now())
        )

    async def delete_session(self, token_hash: str) -> None:
        await self.run("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    async def purge_sessions(self) -> None:
        await self.run("DELETE FROM sessions WHERE expires_at<=?", (_now(),))

    # --- sound_files（実ファイル。中身のハッシュで重複排除） -------------------
    async def get_sound_file(self, digest: str) -> dict | None:
        return await self.one("SELECT * FROM sound_files WHERE hash=?", (digest,))

    async def upsert_sound_file(self, digest: str, ext: str, duration: float, status: str) -> None:
        await self.run(
            """INSERT INTO sound_files(hash, ext, duration, status, created_at) VALUES(?,?,?,?,?)
               ON CONFLICT(hash) DO UPDATE SET ext=excluded.ext, duration=excluded.duration,
                 status=excluded.status""",
            (digest, ext, duration, status, _now()),
        )

    async def set_sound_file_status(self, digest: str, status: str) -> None:
        await self.run("UPDATE sound_files SET status=? WHERE hash=?", (status, digest))

    async def all_sound_files(self) -> list[dict]:
        return await self.all("SELECT * FROM sound_files")

    async def unreferenced_sound_files(self) -> list[dict]:
        return await self.all(
            "SELECT * FROM sound_files f WHERE NOT EXISTS (SELECT 1 FROM sounds s WHERE s.hash=f.hash)"
        )

    async def delete_sound_file(self, digest: str) -> None:
        await self.run("DELETE FROM sound_files WHERE hash=?", (digest,))

    # --- sounds（ユーザーごとの表示名つき） -----------------------------------
    async def list_sounds(self, user_id: int) -> list[dict]:
        return await self.all(
            """SELECT s.id, s.name, s.hash, f.duration, f.status FROM sounds s
               JOIN sound_files f ON f.hash=s.hash WHERE s.user_id=? ORDER BY s.id""",
            (user_id,),
        )

    async def get_sound(self, sound_id: int) -> dict | None:
        return await self.one(
            """SELECT s.*, f.duration, f.status, f.ext FROM sounds s
               JOIN sound_files f ON f.hash=s.hash WHERE s.id=?""",
            (sound_id,),
        )

    async def count_sounds(self, user_id: int) -> int:
        row = await self.one("SELECT COUNT(*) AS n FROM sounds WHERE user_id=?", (user_id,))
        return row["n"]

    async def create_sound(self, user_id: int, name: str, digest: str) -> int:
        cur = await self.run(
            "INSERT INTO sounds(user_id, name, hash, created_at) VALUES(?,?,?,?)",
            (user_id, name, digest, _now()),
        )
        return cur.lastrowid

    async def rename_sound(self, sound_id: int, name: str) -> None:
        await self.run("UPDATE sounds SET name=? WHERE id=?", (name, sound_id))

    async def delete_sound(self, sound_id: int) -> None:
        await self.run("DELETE FROM sounds WHERE id=?", (sound_id,))

    async def hashes_for_alarm(self, alarm_id: int) -> list[str]:
        rows = await self.all(
            """SELECT s.hash FROM alarm_sounds a JOIN sounds s ON s.id=a.sound_id
               WHERE a.alarm_id=? ORDER BY a.position, s.id""",
            (alarm_id,),
        )
        return [r["hash"] for r in rows]

    async def hashes_for_user(self, user_id: int) -> list[str]:
        rows = await self.all("SELECT hash FROM sounds WHERE user_id=? ORDER BY id", (user_id,))
        return [r["hash"] for r in rows]

    # --- alarms -------------------------------------------------------------
    async def list_alarms(self, user_id: int) -> list[dict]:
        return await self.all(
            "SELECT * FROM alarms WHERE user_id=? ORDER BY weekday, start_min", (user_id,)
        )

    async def get_alarm(self, alarm_id: int) -> dict | None:
        return await self.one("SELECT * FROM alarms WHERE id=?", (alarm_id,))

    async def count_alarms(self, user_id: int) -> int:
        row = await self.one("SELECT COUNT(*) AS n FROM alarms WHERE user_id=?", (user_id,))
        return row["n"]

    async def last_channel(self, user_id: int) -> int | None:
        row = await self.one(
            "SELECT channel_id FROM alarms WHERE user_id=? AND channel_id IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        )
        return row["channel_id"] if row else None

    async def create_alarm(self, fields: dict[str, Any]) -> int:
        cols = [c for c in ALARM_COLUMNS if c in fields]
        now = _now()
        sql = (
            f"INSERT INTO alarms({', '.join(cols)}, created_at, updated_at) "
            f"VALUES({', '.join('?' for _ in cols)}, ?, ?)"
        )
        cur = await self.run(sql, [fields[c] for c in cols] + [now, now])
        return cur.lastrowid

    async def update_alarm(self, alarm_id: int, patch: dict[str, Any]) -> None:
        cols = [c for c in ALARM_COLUMNS if c in patch]
        if not cols:
            return
        sets = ", ".join(f"{c}=?" for c in cols)
        await self.run(
            f"UPDATE alarms SET {sets}, updated_at=? WHERE id=?",
            [patch[c] for c in cols] + [_now(), alarm_id],
        )

    async def delete_alarm(self, alarm_id: int) -> None:
        await self.run("DELETE FROM alarms WHERE id=?", (alarm_id,))

    async def group_alarms(self, user_id: int, group_id: str) -> list[dict]:
        return await self.all(
            "SELECT * FROM alarms WHERE user_id=? AND group_id=? ORDER BY weekday", (user_id, group_id)
        )

    async def enabled_alarms(self, user_id: int | None = None) -> list[dict]:
        sql = """SELECT a.* FROM alarms a LEFT JOIN users u ON u.user_id=a.user_id
                 WHERE a.enabled=1 AND a.channel_id IS NOT NULL AND COALESCE(u.paused,0)=0"""
        args: tuple = ()
        if user_id is not None:
            sql += " AND a.user_id=?"
            args = (user_id,)
        return await self.all(sql, args)

    async def others_alarms(self, user_id: int, guild_ids: list[int]) -> list[dict]:
        """同じサーバーの他の人のアラーム。誰のものかは返さない。"""
        if not guild_ids:
            return []
        marks = ", ".join("?" for _ in guild_ids)
        return await self.all(
            f"""SELECT a.weekday, a.start_min, a.end_min, a.join_offset, a.guild_id, a.channel_id
                FROM alarms a LEFT JOIN users u ON u.user_id=a.user_id
                WHERE a.user_id<>? AND a.enabled=1 AND a.channel_id IS NOT NULL
                  AND COALESCE(u.paused,0)=0 AND a.guild_id IN ({marks})""",
            (user_id, *guild_ids),
        )

    # --- alarm_sounds -------------------------------------------------------
    async def alarm_sound_ids(self, alarm_id: int) -> list[int]:
        rows = await self.all(
            "SELECT sound_id FROM alarm_sounds WHERE alarm_id=? ORDER BY position, sound_id", (alarm_id,)
        )
        return [r["sound_id"] for r in rows]

    async def sound_ids_for_alarms(self, alarm_ids: list[int]) -> dict[int, list[int]]:
        result: dict[int, list[int]] = {a: [] for a in alarm_ids}
        if not alarm_ids:
            return result
        marks = ", ".join("?" for _ in alarm_ids)
        rows = await self.all(
            f"SELECT alarm_id, sound_id FROM alarm_sounds WHERE alarm_id IN ({marks}) "
            "ORDER BY position, sound_id",
            alarm_ids,
        )
        for r in rows:
            result[r["alarm_id"]].append(r["sound_id"])
        return result

    async def set_alarm_sounds(self, alarm_id: int, sound_ids: list[int]) -> None:
        await self.conn.execute("DELETE FROM alarm_sounds WHERE alarm_id=?", (alarm_id,))
        await self.conn.executemany(
            "INSERT INTO alarm_sounds(alarm_id, sound_id, position) VALUES(?,?,?)",
            [(alarm_id, sid, i) for i, sid in enumerate(sound_ids)],
        )
        await self.conn.commit()

    # --- overrides / runs ---------------------------------------------------
    async def add_override(self, alarm_id: int, day: date, kind: str = "skip") -> None:
        await self.run(
            "INSERT OR REPLACE INTO alarm_overrides(alarm_id, date, type) VALUES(?,?,?)",
            (alarm_id, day.isoformat(), kind),
        )

    async def skips_between(self, start: date, end: date) -> set[tuple[int, str]]:
        rows = await self.all(
            "SELECT alarm_id, date FROM alarm_overrides WHERE type='skip' AND date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
        return {(r["alarm_id"], r["date"]) for r in rows}

    async def claim_run(self, alarm_id: int, day: date) -> bool:
        """同じ予定を二重に鳴らさないための記録。初回だけ True。"""
        cur = await self.run(
            "INSERT OR IGNORE INTO alarm_runs(alarm_id, date, created_at) VALUES(?,?,?)",
            (alarm_id, day.isoformat(), _now()),
        )
        return cur.rowcount == 1

    async def runs_since(self, start: date) -> set[tuple[int, str]]:
        rows = await self.all("SELECT alarm_id, date FROM alarm_runs WHERE date>=?", (start.isoformat(),))
        return {(r["alarm_id"], r["date"]) for r in rows}

    async def purge_history(self, before: date) -> None:
        await self.run("DELETE FROM alarm_runs WHERE date<?", (before.isoformat(),))
        await self.run("DELETE FROM alarm_overrides WHERE date<?", (before.isoformat(),))
