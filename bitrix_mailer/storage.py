"""Локальная база (SQLite): кампании, очередь получателей и история отправок."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL,          -- идентификатор «этого письма» для истории
    name          TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body_html     TEXT NOT NULL,
    filter_json   TEXT NOT NULL,
    contact_mode  TEXT NOT NULL,
    send_via      TEXT NOT NULL,
    status        TEXT NOT NULL,          -- previewed | confirmed | sending | done | cancelled
    stats_json    TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    confirmed_at  TEXT,
    finished_at   TEXT
);
CREATE TABLE IF NOT EXISTS recipients (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id       INTEGER NOT NULL REFERENCES campaigns(id),
    deal_id           INTEGER NOT NULL,
    deal_title        TEXT,
    contact_id        INTEGER,
    contact_name      TEXT,
    company_id        INTEGER,            -- получатель-компания (у сделки нет контакта)
    is_primary        INTEGER NOT NULL DEFAULT 0,
    email             TEXT,
    status            TEXT NOT NULL,      -- queued | skipped | sending | sent | failed | unknown
    skip_reason       TEXT,
    context_json      TEXT NOT NULL DEFAULT '{}',
    activity_id       TEXT,               -- ID письма/комментария в Timeline
    timeline_verified INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    sent_at           TEXT
);
CREATE INDEX IF NOT EXISTS ix_recipients_campaign ON recipients(campaign_id, status);
CREATE INDEX IF NOT EXISTS ix_recipients_email ON recipients(email, status);
CREATE TABLE IF NOT EXISTS update_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  INTEGER,                -- изменение «после отправки» кампании
    info_json    TEXT NOT NULL,          -- фильтр, что меняем, сколько найдено
    status       TEXT NOT NULL,          -- planned | applied | undone
    created_at   TEXT NOT NULL,
    applied_at   TEXT
);
CREATE TABLE IF NOT EXISTS deal_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL REFERENCES update_batches(id),
    deal_id      INTEGER NOT NULL,
    deal_title   TEXT,
    fields_json  TEXT NOT NULL,          -- новые значения
    before_json  TEXT NOT NULL,          -- старые значения (для отката)
    label        TEXT,                   -- «Стадия: Свободные → В работе»
    status       TEXT NOT NULL,          -- planned | skipped | applied | failed | undone
    skip_reason  TEXT,
    error        TEXT,
    applied_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_deal_changes_batch ON deal_changes(batch_id, status);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(recipients)")}
        if "company_id" not in cols:  # база создана до появления получателей-компаний
            self.db.execute("ALTER TABLE recipients ADD COLUMN company_id INTEGER")
            self.db.commit()

    # --- кампании ---------------------------------------------------------
    def create_campaign(self, *, key, name, subject, body_html, filter_info, contact_mode, send_via) -> int:
        cur = self.db.execute(
            "INSERT INTO campaigns(key,name,subject,body_html,filter_json,contact_mode,send_via,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,'previewed',?)",
            (key, name, subject, body_html, json.dumps(filter_info, ensure_ascii=False), contact_mode, send_via, now()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def campaign(self, campaign_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise KeyError(f"кампания {campaign_id} не найдена")
        return row

    def set_status(self, campaign_id: int, status: str) -> None:
        extra = {"confirmed": ", confirmed_at=?", "done": ", finished_at=?"}.get(status, "")
        args = (status, now(), campaign_id) if extra else (status, campaign_id)
        self.db.execute(f"UPDATE campaigns SET status=?{extra} WHERE id=?", args)
        self.db.commit()

    def set_stats(self, campaign_id: int, stats: dict) -> None:
        self.db.execute("UPDATE campaigns SET stats_json=? WHERE id=?", (json.dumps(stats, ensure_ascii=False), campaign_id))
        self.db.commit()

    def campaigns(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()

    # --- получатели -------------------------------------------------------
    def add_recipients(self, campaign_id: int, rows: list[dict]) -> None:
        self.db.executemany(
            "INSERT INTO recipients(campaign_id,deal_id,deal_title,contact_id,contact_name,company_id,is_primary,email,"
            "status,skip_reason,context_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (campaign_id, r["deal_id"], r.get("deal_title"), r.get("contact_id"), r.get("contact_name"), r.get("company_id"),
                 int(bool(r.get("is_primary"))), r.get("email"), r["status"], r.get("skip_reason"),
                 json.dumps(r.get("context") or {}, ensure_ascii=False))
                for r in rows
            ],
        )
        self.db.commit()

    def recipients(self, campaign_id: int, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.db.execute("SELECT * FROM recipients WHERE campaign_id=? AND status=? ORDER BY id",
                                   (campaign_id, status)).fetchall()
        return self.db.execute("SELECT * FROM recipients WHERE campaign_id=? ORDER BY id", (campaign_id,)).fetchall()

    def update_recipient(self, rid: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE recipients SET {cols} WHERE id=?", (*fields.values(), rid))
        self.db.commit()

    def counts(self, campaign_id: int) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) n FROM recipients WHERE campaign_id=? GROUP BY status",
                               (campaign_id,)).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # --- изменения сделок ---------------------------------------------------
    def create_update_batch(self, info: dict, rows: list[dict], campaign_id: int | None = None) -> int:
        cur = self.db.execute("INSERT INTO update_batches(campaign_id,info_json,status,created_at) VALUES (?,?,'planned',?)",
                              (campaign_id, json.dumps(info, ensure_ascii=False), now()))
        bid = int(cur.lastrowid)
        self.db.executemany(
            "INSERT INTO deal_changes(batch_id,deal_id,deal_title,fields_json,before_json,label,status,skip_reason)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(bid, r["deal_id"], r.get("deal_title"), json.dumps(r["fields"], ensure_ascii=False),
              json.dumps(r["before"], ensure_ascii=False), r.get("label"), r["status"], r.get("skip_reason"))
             for r in rows],
        )
        self.db.commit()
        return bid

    def update_batch(self, batch_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM update_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise KeyError(f"изменение #{batch_id} не найдено")
        return row

    def update_batches(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM update_batches ORDER BY id DESC").fetchall()

    def campaign_update_batch(self, campaign_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM update_batches WHERE campaign_id=? ORDER BY id DESC LIMIT 1",
                               (campaign_id,)).fetchone()

    def set_update_status(self, batch_id: int, status: str) -> None:
        self.db.execute("UPDATE update_batches SET status=?, applied_at=COALESCE(applied_at, ?) WHERE id=?",
                        (status, now(), batch_id))
        self.db.commit()

    def deal_changes(self, batch_id: int, status: str | None = None, deal_id: int | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM deal_changes WHERE batch_id=?", [batch_id]
        if status:
            sql, args = sql + " AND status=?", args + [status]
        if deal_id is not None:
            sql, args = sql + " AND deal_id=?", args + [deal_id]
        return self.db.execute(sql + " ORDER BY id", args).fetchall()

    def update_deal_change(self, cid: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE deal_changes SET {cols} WHERE id=?", (*fields.values(), cid))
        self.db.commit()

    def change_counts(self, batch_id: int) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) n FROM deal_changes WHERE batch_id=? GROUP BY status",
                               (batch_id,)).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # --- история ----------------------------------------------------------
    def already_sent(self, key: str, email: str, exclude_recipient: int | None = None) -> sqlite3.Row | None:
        """Получал ли адрес письмо с тем же ключом кампании (в любой из кампаний)."""
        return self.db.execute(
            "SELECT r.*, c.id AS cid FROM recipients r JOIN campaigns c ON c.id=r.campaign_id "
            "WHERE c.key=? AND r.email=? AND r.status IN ('sent','sending','unknown') AND r.id != ? "
            "ORDER BY r.sent_at DESC LIMIT 1",
            (key, email, exclude_recipient or -1),
        ).fetchone()
