# -*- coding: utf-8 -*-
"""RSS/Atom 增量采集：读取 config.yaml 的 rss_feeds，多线程抓取、解析、打健康标记。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import feedparser

from ..config import load_config, get_proxy
from ..fetcher import get_session, polite_sleep
from ..normalize import parse_date_flexible, strip_html, guess_language, fmt_date
from .. import store


def _entry_date(entry) -> tuple:
    """返回 (datetime|None, 原始字符串)。优先结构化字段，再退化解析字符串。"""
    for attr in ("published_parsed", "updated_parsed"):
        v = getattr(entry, attr, None)
        if v:
            try:
                return datetime(*v[:6]), getattr(entry, attr.replace("_parsed", ""), "")
            except Exception:
                pass
    for attr in ("published", "updated"):
        v = getattr(entry, attr, None)
        if v:
            dt = parse_date_flexible(v)
            if dt:
                return dt, v
    return None, ""


def _entry_summary(entry) -> str:
    s = (entry.get("summary") or "").strip()
    if not s:
        content = getattr(entry, "content", None)
        if content and isinstance(content, list) and content:
            s = content[0].get("value", "")
    return strip_html(s, limit=2000)


def fetch_one_feed(feed_conf: dict, start_date_dt: datetime, keywords_cfg: dict,
                   check_only: bool = False) -> dict:
    """抓取单个源。返回 {status, entries_seen, new, detail}；结果不直接写库，由调用方汇总。"""
    name = feed_conf["name"]
    url = feed_conf["url"]
    result = {"source": name, "status": "ok", "entries_seen": 0, "new": 0, "detail": "",
              "needs_proxy": bool(feed_conf.get("needs_proxy"))}

    try:
        session = get_session()
        try:
            resp = session.get(url, timeout=load_config()["settings"]["timeout"],
                               headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        finally:
            session.close()

        feed = feedparser.parse(resp.content)
        entries = feed.entries or []
        result["entries_seen"] = len(entries)

        if check_only:
            return result

        for entry in entries:
            pub_dt, _raw = _entry_date(entry)
            title = strip_html(entry.get("title", ""))
            if not title:
                continue
            # 时间过滤：早于研究窗口的丢弃；无日期的保留（导出时单独归"日期待定"）
            if pub_dt and pub_dt < start_date_dt:
                continue
            rec = {
                "title": title,
                "url": entry.get("link", ""),
                "summary": _entry_summary(entry),
                "jurisdiction": feed_conf.get("jurisdiction", ""),
                "jurisdiction_name": feed_conf.get("jurisdiction_name", ""),
                "level": feed_conf.get("level", ""),
                "issuing_body": feed_conf.get("issuing_body", ""),
                "policy_type": feed_conf.get("policy_type", ""),
                "language": feed_conf.get("language") or guess_language(title),
                "published_date": fmt_date(pub_dt),
                "source_name": name,
                "collection_method": "rss",
                "extra": {"feed_title": feed.feed.get("title", "") if getattr(feed, "feed", None) else ""},
            }
            if store.insert_policy(rec, keywords_cfg) == "new":
                result["new"] += 1
        return result

    except Exception as e:  # noqa: BLE001 — 每源独立失败，不影响其他源
        result["status"] = "failed"
        result["detail"] = f"{type(e).__name__}: {e}"
        if result["needs_proxy"] and not get_proxy():
            result["detail"] += "（该源需代理：请在 config.yaml settings.proxy 填写或设置 HTTPS_PROXY 环境变量后重试）"
        return result


def run(check_only: bool = False) -> dict:
    """主入口：并发抓取所有启用的 RSS 源，写健康记录，返回汇总。"""
    cfg = load_config()
    start_dt = cfg["settings"]["start_date_dt"]
    kw = cfg["_keywords"]
    feeds = [f for f in (cfg.get("rss_feeds") or []) if f.get("enabled", True)]

    results = []
    with ThreadPoolExecutor(max_workers=cfg["settings"]["max_workers"]) as pool:
        futures = {pool.submit(fetch_one_feed, f, start_dt, kw, check_only): f for f in feeds}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            mark = "✅" if r["status"] == "ok" else "❌"
            extra = f" +{r['new']} 新增" if not check_only else ""
            print(f"  {mark} {r['source']}: {r['entries_seen']} 条{extra} {r['detail']}")
            polite_sleep()

    for r in results:
        store.record_health(r["source"], next((f["url"] for f in feeds if f["name"] == r["source"]), ""),
                         "ok" if r["status"] == "ok" else "failed",
                         r["detail"], r["entries_seen"], r.get("new", 0))

    ok = sum(1 for r in results if r["status"] == "ok")
    return {"ok": ok, "failed": len(results) - ok, "new": sum(r.get("new", 0) for r in results),
            "results": results}
