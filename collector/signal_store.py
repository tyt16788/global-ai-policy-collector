# -*- coding: utf-8 -*-
"""信号层独立存储：`data/signal_master.csv`（企业博客 = 六大巨头的官方发声）。

为什么必须单独一张表
--------------------
政策主表回答的是"政府说了什么"，信号表回答的是"企业说了什么"。
如果混在一起，"美国 1461 条"里会混进 OpenAI 博客，国家分布和年份分布全失真，
论文里的统计口径就说不清了。两表**结构同构**（复用 `store.COLUMNS`），
额外多 4 列用于区分信号属性：

    source_type   policy / corporate_blog
    signal_layer  governance（治理/政策主张） / tech（技术/产品发布）
    company       OpenAI / Google DeepMind / NVIDIA / Anthropic / Meta / Microsoft
    topic_tags    主题标签（政策法规、安全治理、产品发布……），逗号分隔

全文与政策层同一口径：`fulltext_status == "ok"` 才算"全文已入库可用记录"，
只拿到 RSS 摘要的记录标 `failed:*`，下一轮自动重试。
"""

import json
import os
import threading
import time
from datetime import datetime

import pandas as pd

from .config import load_config, resolve_path
from .normalize import make_content_hash, make_guid, tag_ai
from .store import AUTOSAVE_EVERY, COLUMNS, FULL_TEXT_MAX_CHARS

# 信号层额外列（放在政策同构列之后）
SIGNAL_EXTRA_COLUMNS = ["source_type", "signal_layer", "company", "topic_tags"]
SIGNAL_COLUMNS = COLUMNS + SIGNAL_EXTRA_COLUMNS

_master = None
_guid_index: set = set()
_new_since_save = 0
_lock = threading.RLock()


def signal_path():
    rel = load_config()["settings"].get("signal_csv", "data/signal_master.csv")
    return resolve_path(rel)


def health_path():
    rel = load_config()["settings"].get("signal_health_csv", "data/signal_health.csv")
    return resolve_path(rel)


def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in SIGNAL_COLUMNS})


def load(force: bool = False) -> pd.DataFrame:
    global _master, _guid_index
    with _lock:
        if _master is not None and not force:
            return _master
        path = signal_path()
        if path.exists() and path.stat().st_size > 0:
            df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            for col in SIGNAL_COLUMNS:
                if col not in df.columns:
                    df[col] = ""
            _master = df[SIGNAL_COLUMNS]
        else:
            _master = _empty()
        _guid_index = set(_master["guid"])
        return _master


def save(retries: int = 3):
    global _new_since_save
    with _lock:
        if _master is None:
            return
        path = signal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".csv.tmp")
        for attempt in range(retries):
            try:
                _master.to_csv(tmp, index=False, encoding="utf-8-sig")
                os.replace(tmp, path)
                _new_since_save = 0
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(2 * (attempt + 1))


def _maybe_autosave():
    global _new_since_save
    if _new_since_save >= AUTOSAVE_EVERY:
        save()


def insert(rec: dict, keywords_cfg: dict = None) -> str:
    """插入一条信号记录。返回 'new' / 'duplicate' / 'skipped_no_title'。"""
    global _new_since_save
    title = (rec.get("title") or "").strip()
    if not title:
        return "skipped_no_title"
    url = (rec.get("url") or "").strip()
    pub = (rec.get("published_date") or "").strip()
    guid = make_guid(url, title, pub)

    with _lock:
        load()
        if guid in _guid_index:
            return "duplicate"
        jurisdiction = rec.get("jurisdiction", "")
        text_for_tags = " ".join(filter(None, [title, rec.get("summary", "")]))
        row = {c: "" for c in SIGNAL_COLUMNS}
        row.update({
            "guid": guid,
            "title": title,
            "url": url,
            "summary": (rec.get("summary") or "")[:2000],
            "full_text": (rec.get("full_text") or "")[:FULL_TEXT_MAX_CHARS],
            "jurisdiction": jurisdiction,
            "jurisdiction_name": rec.get("jurisdiction_name", ""),
            "level": rec.get("level", ""),
            "issuing_body": rec.get("issuing_body", ""),
            "policy_type": rec.get("policy_type", ""),
            "language": rec.get("language", ""),
            "published_date": pub,
            "source_name": rec.get("source_name", ""),
            "collection_method": rec.get("collection_method", ""),
            "ai_tags": rec.get("ai_tags") or tag_ai(text_for_tags, keywords_cfg or {}),
            "fulltext_status": rec.get("fulltext_status", ""),
            "content_hash": make_content_hash(jurisdiction, title, pub),
            "extra_json": json.dumps(rec.get("extra") or {}, ensure_ascii=False),
            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_type": rec.get("source_type", "corporate_blog"),
            "signal_layer": rec.get("signal_layer", ""),
            "company": rec.get("company", ""),
            "topic_tags": rec.get("topic_tags", ""),
        })
        _master.loc[len(_master)] = row
        _guid_index.add(guid)
        _new_since_save += 1
    _maybe_autosave()
    return "new"


def set_fulltext(guid: str, text: str, status: str = "ok") -> bool:
    with _lock:
        load()
        idx = _master.index[_master["guid"] == guid]
        if len(idx) == 0:
            return False
        _master.loc[idx[0], "full_text"] = (text or "")[:FULL_TEXT_MAX_CHARS]
        _master.loc[idx[0], "fulltext_status"] = status
        _master.loc[idx[0], "fulltext_crawled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return True


def set_title(guid: str, title: str) -> bool:
    with _lock:
        load()
        idx = _master.index[_master["guid"] == guid]
        if len(idx) == 0:
            return False
        _master.loc[idx[0], "title"] = (title or "")[:400]
    return True


def set_classification(guid: str, layer: str, tags: str) -> bool:
    """离线重算分层/标签时用（规则调整后回溯修正历史记录）。"""
    with _lock:
        load()
        idx = _master.index[_master["guid"] == guid]
        if len(idx) == 0:
            return False
        _master.loc[idx[0], "signal_layer"] = layer
        _master.loc[idx[0], "topic_tags"] = tags
    return True


def set_date(guid: str, date_str: str) -> bool:
    """补/修发布日期（列表页解析漏掉日期时回溯补）。"""
    with _lock:
        load()
        idx = _master.index[_master["guid"] == guid]
        if len(idx) == 0:
            return False
        _master.loc[idx[0], "published_date"] = date_str or ""
    return True


def missing_fulltext(limit: int = 5000) -> pd.DataFrame:
    """全文未 ok 且有 url 的记录，供下一轮补抓。"""
    with _lock:
        df = load()
        m = (df["fulltext_status"] != "ok") & (df["url"] != "")
        return df.loc[m, ["guid", "url", "company", "source_name", "collection_method"]].head(limit).copy()


def stats() -> dict:
    with _lock:
        df = load()
    total = len(df)
    ok = int((df["fulltext_status"] == "ok").sum())
    return {
        "total": total,
        "fulltext_ok": ok,
        "fulltext_failed": int(df["fulltext_status"].str.startswith("failed").sum()),
        "by_company": df["company"].replace("", "未知").value_counts().to_dict(),
        "by_layer": df["signal_layer"].replace("", "未知").value_counts().to_dict(),
        "by_year": df["published_date"].str.slice(0, 4).replace("", "未知").value_counts().sort_index().to_dict(),
    }


def record_health(source_name: str, url: str, status: str, detail: str = "",
                  entries_seen: int = 0, entries_new: int = 0):
    path = health_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "check_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_name": source_name, "url": url, "status": status,
        "detail": (detail or "")[:300], "entries_seen": entries_seen, "entries_new": entries_new,
    }
    header = not path.exists()
    pd.DataFrame([row]).to_csv(path, mode="a", header=header, index=False, encoding="utf-8-sig")
