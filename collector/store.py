# -*- coding: utf-8 -*-
"""CSV 存储层：单一主表 policy_master.csv（含全文列），guid 去重追加。

设计：
- 主表常驻内存（pandas DataFrame），insert 只操作内存，定期/命令结束时落盘；
- source_health.csv / runs.csv 为追加式日志；
- 不使用 SQL/SQLite。
注意：主表含全文列后体积较大，请勿用 Excel 打开后直接保存（会静默截断超长单元格）。
"""

import json
import os
import threading
import time
from datetime import datetime
from typing import Optional

import pandas as pd

from .config import load_config, resolve_path
from .normalize import make_guid, make_content_hash, normalize_url, tag_ai

COLUMNS = [
    "guid", "title", "url", "summary", "full_text",
    "jurisdiction", "jurisdiction_name", "level", "issuing_body", "policy_type",
    "language", "published_date", "source_name", "collection_method",
    "ai_tags", "fulltext_status", "content_hash", "extra_json",
    "collected_at", "fulltext_crawled_at",
]

FULL_TEXT_MAX_CHARS = 50_000    # 单条全文上限（覆盖 p90 以上），控制主表体积，极端长文可从 url 溯源
AUTOSAVE_EVERY = 500            # 每累计 N 条新数据自动落盘一次

_lock = threading.RLock()
_master: Optional[pd.DataFrame] = None
_guid_index: set = set()
_new_since_save = 0


def master_path():
    return resolve_path(load_config()["settings"].get("master_csv", "data/policy_master.csv"))


def health_path():
    return resolve_path("data/source_health.csv")


def runs_path():
    return resolve_path("data/runs.csv")


def _empty_master() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS).astype({c: "object" for c in COLUMNS})


def load_master(force: bool = False) -> pd.DataFrame:
    """加载主表到内存（单例）。CSV 不存在则返回空表。"""
    global _master, _guid_index
    with _lock:
        if _master is not None and not force:
            return _master
        path = master_path()
        if path.exists() and path.stat().st_size > 0:
            df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            for col in COLUMNS:
                if col not in df.columns:
                    df[col] = ""
            _master = df[COLUMNS]
        else:
            _master = _empty_master()
        _guid_index = set(_master["guid"])
        return _master


def save(retries: int = 3, merge: bool = True):
    """把内存主表写回 CSV（utf-8-sig，便于 Excel 识别编码）。

    Windows 下若文件被其他进程（如 Excel/读脚本）占用，先写临时文件再原子替换，
    仍失败则退避重试，避免长跑任务因偶发读冲突丢数据。

    merge=True 时会先读磁盘上的最新版本，把本进程没改过的行以磁盘为准
    （按 guid 对齐），避免"长跑任务结束时整表覆盖"丢掉另一进程同时写入的数据
    —— 2026-10-19 就发生过：fulltext 长跑 2 小时后保存，把我同时段写入的 281 条
    OECD 概述全文整表覆盖掉了。
    """
    global _new_since_save, _master
    with _lock:
        if _master is None:
            return
        path = master_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".csv.tmp")

        if merge and path.exists() and path.stat().st_size > 0:
            try:
                disk = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
                for col in COLUMNS:
                    if col not in disk.columns:
                        disk[col] = ""
                disk = disk[COLUMNS]
                disk_idx = disk.set_index("guid", drop=False)
                mine_idx = _master.set_index("guid", drop=False)
                # 磁盘有、本进程没有的行 → 补回来（别的进程新写的）
                extra = disk_idx[~disk_idx.index.isin(mine_idx.index)]
                # 两边都有的行 → 以本进程内存为准（它可能刚更新过全文）
                merged = pd.concat([mine_idx, extra]) if len(extra) else mine_idx
                _master = merged.reset_index(drop=True)
            except Exception:  # noqa: BLE001 — 合并失败就退回整表覆盖，不阻断保存
                pass

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


def insert_policy(rec: dict, keywords_cfg: dict) -> str:
    """插入一条政策记录。返回 'new' / 'duplicate' / 'skipped_no_title'。"""
    global _new_since_save
    title = (rec.get("title") or "").strip()
    if not title:
        return "skipped_no_title"
    url = (rec.get("url") or "").strip()
    pub = (rec.get("published_date") or "").strip()
    guid = make_guid(url, title, pub)

    with _lock:
        load_master()
        if guid in _guid_index:
            return "duplicate"
        jurisdiction = rec.get("jurisdiction", "")
        text_for_tags = " ".join(filter(None, [title, rec.get("summary", "")]))
        row = {c: "" for c in COLUMNS}
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
            "ai_tags": tag_ai(text_for_tags, keywords_cfg),
            "fulltext_status": rec.get("fulltext_status", ""),
            "content_hash": make_content_hash(jurisdiction, title, pub),
            "extra_json": json.dumps(rec.get("extra") or {}, ensure_ascii=False),
            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        _master.loc[len(_master)] = row
        _guid_index.add(guid)
        _new_since_save += 1
    _maybe_autosave()
    return "new"


def set_fulltext(guid: str, text: str, status: str = "ok"):
    """回填某条记录的全文（全文抓取线程调用，线程安全）。"""
    with _lock:
        load_master()
        idx = _master.index[_master["guid"] == guid]
        if len(idx) == 0:
            return False
        _master.loc[idx[0], "full_text"] = (text or "")[:FULL_TEXT_MAX_CHARS]
        _master.loc[idx[0], "fulltext_status"] = status
        _master.loc[idx[0], "fulltext_crawled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return True


def patch_extra(guid: str, extra: dict):
    """合并写入 extra_json 的若干键（不覆盖已有键）。"""
    with _lock:
        load_master()
        idx = _master.index[_master["guid"] == guid]
        if len(idx) == 0:
            return False
        try:
            cur = json.loads(_master.loc[idx[0], "extra_json"] or "{}")
        except json.JSONDecodeError:
            cur = {}
        if not isinstance(cur, dict):
            cur = {}
        cur.update(extra or {})
        _master.loc[idx[0], "extra_json"] = json.dumps(cur, ensure_ascii=False)
    return True


def records_missing_fulltext(limit: int = 1000) -> "pd.DataFrame":
    """fulltext_status 不以 ok 开头且有 url 的记录（失败的下轮自动重试）。

    注意：`ok:oecd_overview` 这类带后缀的状态也算已抓，不再重试。
    """
    with _lock:
        df = load_master()
        m = (~df["fulltext_status"].str.startswith("ok")) & (df["url"] != "")
        return df.loc[m, ["guid", "url", "collection_method", "extra_json"]].head(limit).copy()


def stats() -> dict:
    with _lock:
        df = load_master()
    total = len(df)
    dated = df[df["published_date"] != ""]
    status = df["fulltext_status"].fillna("")
    desc_only = status == "ok:oecd_desc_only"
    external = status == "ok:oecd_overview"
    original = status == "ok"
    # "有可用正文" = 政府原文 + OECD 官方概述；只有短摘兜底的不算
    usable = (original | external)
    return {
        "total": total,
        "no_date": int((df["published_date"] == "").sum()),
        "fulltext_ok": int(usable.sum()),
        "fulltext_ok_original": int(original.sum()),
        "fulltext_ok_external": int(external.sum()),
        "fulltext_desc_only": int(desc_only.sum()),
        "fulltext_failed": int(status.str.startswith("failed").sum()),
        "by_jurisdiction": df["jurisdiction"].replace("", "未知").value_counts().to_dict(),
        "by_year": dated["published_date"].str[:4].value_counts().sort_index().to_dict(),
        "by_method": df["collection_method"].replace("", "未知").value_counts().to_dict(),
    }


# ---------------- 健康与运行日志（追加式 CSV） ----------------
def _append_csv(path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row])
    header = not path.exists()
    df.to_csv(path, mode="a", header=header, index=False, encoding="utf-8-sig")


def record_health(source_name: str, url: str, status: str, detail: str = "",
                  entries_seen: int = 0, entries_new: int = 0):
    _append_csv(health_path(), {
        "check_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_name": source_name, "url": url, "status": status,
        "detail": (detail or "")[:300], "entries_seen": entries_seen, "entries_new": entries_new,
    })


def log_run(command: str, sources_ok: int, sources_failed: int, items_new: int, detail: str = ""):
    _append_csv(runs_path(), {
        "command": command,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sources_ok": sources_ok, "sources_failed": sources_failed, "items_new": items_new,
        "detail": (detail or "")[:500],
    })


def latest_health() -> list:
    path = health_path()
    if not path.exists():
        return []
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    return df.drop_duplicates(subset=["source_name"], keep="last").to_dict("records")


def suspected_duplicates() -> list:
    with _lock:
        df = load_master()
    df = df[df["content_hash"] != ""]
    if df.empty:
        return []
    grp = df.groupby("content_hash")
    out = []
    for h, g in grp:
        if len(g) > 1:
            first = g.iloc[0]
            out.append({
                "jurisdiction": first["jurisdiction"], "title": first["title"],
                "published_date": first["published_date"], "n": len(g),
                "urls": " | ".join(g["url"].head(5)),
            })
    return sorted(out, key=lambda x: -x["n"])[:200]
