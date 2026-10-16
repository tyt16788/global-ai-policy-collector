# -*- coding: utf-8 -*-
"""SQLite 数据层：政策主表（URL 规范化去重）、源健康表、运行日志、统计查询。"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import load_config, resolve_path
from .normalize import make_guid, make_content_hash, normalize_url, tag_ai

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    url_normalized TEXT,
    summary TEXT,
    jurisdiction TEXT,
    jurisdiction_name TEXT,
    level TEXT,
    issuing_body TEXT,
    policy_type TEXT,
    language TEXT,
    published_date TEXT,
    collected_at TEXT,
    source_name TEXT,
    collection_method TEXT,
    full_text_path TEXT,
    content_hash TEXT,
    ai_tags TEXT,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_policies_juris ON policies(jurisdiction);
CREATE INDEX IF NOT EXISTS idx_policies_pubdate ON policies(published_date);
CREATE INDEX IF NOT EXISTS idx_policies_content ON policies(content_hash);

CREATE TABLE IF NOT EXISTS source_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_time TEXT,
    source_name TEXT,
    url TEXT,
    status TEXT,
    detail TEXT,
    entries_seen INTEGER DEFAULT 0,
    entries_new INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT,
    started_at TEXT,
    finished_at TEXT,
    sources_ok INTEGER,
    sources_failed INTEGER,
    items_new INTEGER,
    detail_json TEXT
);
"""


def _connect() -> sqlite3.Connection:
    db_file = resolve_path(load_config()["settings"]["db_path"])
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def insert_policy(rec: dict, keywords_cfg: dict) -> str:
    """
    插入一条政策记录（规范化字段名见 normalize/canonical keys）。
    返回 'new' / 'duplicate'。guid 冲突时忽略。
    """
    url = (rec.get("url") or "").strip()
    pub = (rec.get("published_date") or "").strip()
    guid = make_guid(url, rec.get("title", ""), pub)
    title = (rec.get("title") or "").strip()
    if not title:
        return "skipped_no_title"

    text_for_tags = " ".join(filter(None, [title, rec.get("summary", "")]))
    jurisdiction = rec.get("jurisdiction", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "guid": guid,
        "title": title,
        "url": url,
        "url_normalized": normalize_url(url),
        "summary": (rec.get("summary") or "")[:2000],
        "jurisdiction": jurisdiction,
        "jurisdiction_name": rec.get("jurisdiction_name", ""),
        "level": rec.get("level", ""),
        "issuing_body": rec.get("issuing_body", ""),
        "policy_type": rec.get("policy_type", ""),
        "language": rec.get("language", ""),
        "published_date": pub,
        "collected_at": now,
        "source_name": rec.get("source_name", ""),
        "collection_method": rec.get("collection_method", ""),
        "full_text_path": rec.get("full_text_path", ""),
        "content_hash": make_content_hash(jurisdiction, title, pub),
        "ai_tags": tag_ai(text_for_tags, keywords_cfg),
        "extra_json": json.dumps(rec.get("extra") or {}, ensure_ascii=False),
    }

    with _connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO policies
               (guid, title, url, url_normalized, summary, jurisdiction, jurisdiction_name,
                level, issuing_body, policy_type, language, published_date, collected_at,
                source_name, collection_method, full_text_path, content_hash, ai_tags, extra_json)
               VALUES (:guid, :title, :url, :url_normalized, :summary, :jurisdiction, :jurisdiction_name,
                :level, :issuing_body, :policy_type, :language, :published_date, :collected_at,
                :source_name, :collection_method, :full_text_path, :content_hash, :ai_tags, :extra_json)""",
            row,
        )
        return "new" if cur.rowcount else "duplicate"


def record_health(source_name: str, url: str, status: str, detail: str = "",
                  entries_seen: int = 0, entries_new: int = 0):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO source_health (check_time, source_name, url, status, detail, entries_seen, entries_new) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source_name, url, status, detail,
             entries_seen, entries_new),
        )


def start_run(command: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (command, started_at) VALUES (?, ?)",
            (command, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return cur.lastrowid


def finish_run(run_id: int, sources_ok: int, sources_failed: int, items_new: int, detail: dict):
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, sources_ok=?, sources_failed=?, items_new=?, detail_json=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sources_ok, sources_failed,
             items_new, json.dumps(detail, ensure_ascii=False), run_id),
        )


# ---------------- 查询 ----------------
def stats() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0]
        by_juris = conn.execute(
            "SELECT jurisdiction, COUNT(*) n FROM policies GROUP BY jurisdiction ORDER BY n DESC").fetchall()
        by_year = conn.execute(
            "SELECT substr(published_date,1,4) y, COUNT(*) n FROM policies WHERE published_date!='' GROUP BY y ORDER BY y").fetchall()
        no_date = conn.execute(
            "SELECT COUNT(*) FROM policies WHERE published_date=''").fetchone()[0]
        by_method = conn.execute(
            "SELECT collection_method, COUNT(*) n FROM policies GROUP BY collection_method ORDER BY n DESC").fetchall()
        return {
            "total": total,
            "by_jurisdiction": {r["jurisdiction"]: r["n"] for r in by_juris},
            "by_year": {r["y"]: r["n"] for r in by_year},
            "no_date": no_date,
            "by_method": {r["collection_method"]: r["n"] for r in by_method},
        }


def latest_health(limit: int = 100) -> list:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT h.* FROM source_health h
               JOIN (SELECT source_name, MAX(id) mid FROM source_health GROUP BY source_name) t
               ON h.source_name = t.source_name AND h.id = t.mid
               ORDER BY h.id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


def fetch_all_for_export(since_date: str) -> tuple:
    """返回 (有日期且>=since_date 的记录, 日期缺失的记录)。"""
    q = """SELECT title, url, summary, jurisdiction, jurisdiction_name, level, issuing_body,
                  policy_type, language, published_date, source_name, collection_method,
                  full_text_path, ai_tags, content_hash
           FROM policies"""
    with _connect() as conn:
        dated = conn.execute(q + " WHERE published_date != '' ORDER BY published_date DESC").fetchall()
        undated = conn.execute(q + " WHERE published_date = '' ORDER BY collected_at DESC").fetchall()
    keep = [dict(r) for r in dated if r["published_date"] >= since_date]
    return keep, [dict(r) for r in undated]


def suspected_duplicates() -> list:
    """同一 (国家, 标题, 日期) 出现在多个 URL 的疑似重复组。"""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT content_hash, jurisdiction, title, published_date, COUNT(*) n,
                      GROUP_CONCAT(url, ' | ') urls
               FROM policies WHERE content_hash != ''
               GROUP BY content_hash HAVING n > 1 ORDER BY n DESC LIMIT 200""").fetchall()
        return [dict(r) for r in rows]


def policies_missing_fulltext(limit: int = 1000) -> list:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT guid, url FROM policies
               WHERE (full_text_path IS NULL OR full_text_path='') AND url != ''
               ORDER BY published_date DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


def set_fulltext_path(guid: str, path: str):
    with _connect() as conn:
        conn.execute("UPDATE policies SET full_text_path=? WHERE guid=?", (path, guid))
