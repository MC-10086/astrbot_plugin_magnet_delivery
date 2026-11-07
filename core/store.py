"""Small transactional SQLite store. All access is on the event-loop thread."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .models import ACTIVE


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, chat TEXT NOT NULL, owner TEXT NOT NULL,
                backend TEXT NOT NULL, infohash TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS tasks_chat ON tasks(chat, created);
            CREATE TABLE IF NOT EXISTS settings(chat TEXT PRIMARY KEY, body TEXT NOT NULL);
        """)

    def add(self, data: dict) -> dict:
        data = dict(
            data,
            id=uuid.uuid4().hex,
            created=time.time(),
            state="queued",
            remote_id="",
            total=0,
            done=0,
            speed=0,
            error="",
            cancel=False,
        )
        self.save(data)
        return data

    def save(self, task: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?,?)",
            (
                task["id"],
                task["chat"],
                task["owner"],
                task["backend"],
                task["infohash"],
                task["state"],
                task["created"],
                json.dumps(task),
            ),
        )
        self.db.commit()

    def get(self, task_id: str) -> dict | None:
        row = self.db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, chat: str | None = None, active: bool = False) -> list[dict]:
        where, args = [], []
        if chat is not None:
            where.append("chat=?")
            args.append(chat)
        if active:
            where.append("state IN (" + ",".join("?" * len(ACTIVE)) + ")")
            args.extend(ACTIVE)
        sql = "SELECT body FROM tasks" + (" WHERE " + " AND ".join(where) if where else "")
        return [json.loads(row[0]) for row in self.db.execute(sql + " ORDER BY created", args)]

    def settings(self, chat: str) -> dict:
        row = self.db.execute("SELECT body FROM settings WHERE chat=?", (chat,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_setting(self, chat: str, key: str, value):
        settings = self.settings(chat)
        settings[key] = value
        self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (chat, json.dumps(settings)))
        self.db.commit()

    def resolve(self, chat: str, prefix: str) -> dict:
        if len(prefix) < 8:
            raise ValueError("任务编号至少输入前8位")
        found = [t for t in self.list(chat) if t["id"].startswith(prefix)]
        if len(found) != 1:
            raise ValueError("当前聊天中没有唯一匹配的任务")
        return found[0]

    def close(self):
        self.db.close()
