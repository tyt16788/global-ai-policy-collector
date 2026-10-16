# -*- coding: utf-8 -*-
"""gov.cn 国务院政策文件库回填：官方检索 API（直连可用）。
t=zhengcelibrary_gw 国务院文件 / t=zhengcelibrary_bm 部委文件。
字段实测：title/pcode(文号)/pubtimeStr/puborg(发文机构)/url/summary/childtype。
"""

import time
import random
from urllib.parse import quote

from ..config import load_config
from ..fetcher import get_session
from ..normalize import strip_html, parse_date_flexible, fmt_date
from .. import store

# 检索字段：标题 + 全文，两者并跑，覆盖面最大
SEARCH_FIELDS = ["title", "content"]


def _api_url(base: str, t: str, keyword: str, field: str, page: int, page_size: int) -> str:
    return (f"{base}?t={t}&q={quote(keyword)}&timetype=timeqb"
            f"&sort=pubtime&sortType=1&searchfield={field}"
            f"&p={page}&n={page_size}")


def _parse_record(rec: dict, t: str) -> dict:
    """把接口原始记录转成标准入库字段。"""
    title = strip_html(rec.get("title", ""))
    pub_dt = parse_date_flexible(rec.get("pubtimeStr", ""))
    return {
        "title": title,
        "url": rec.get("url", ""),
        "summary": strip_html(rec.get("summary", ""), limit=2000),
        "jurisdiction": "CN",
        "jurisdiction_name": "中国",
        "level": "中央",
        "issuing_body": rec.get("puborg", "") or ("国务院" if t.endswith("_gw") else ""),
        "policy_type": "政策文件",
        "language": "zh",
        "published_date": fmt_date(pub_dt),
        "source_name": "gov.cn政策文件库",
        "collection_method": "govcn_api",
        "extra": {
            "文号": rec.get("pcode", ""),
            "分类": rec.get("childtype", ""),
            "索引号": rec.get("index", ""),
            "检索范围": "国务院文件" if t.endswith("_gw") else "部委文件",
        },
    }


def run() -> dict:
    cfg = load_config()
    g = cfg.get("govcn") or {}
    if not g.get("enabled", True):
        return {"ok": 0, "failed": 0, "new": 0, "results": []}

    base = g["base"]
    collections = g.get("collections", [])
    keywords = g.get("keywords", [])
    page_size = int(g.get("page_size", 100))
    max_pages = int(g.get("max_pages", 20))
    start_dt = cfg["settings"]["start_date_dt"]
    kw_cfg = cfg["_keywords"]

    session = get_session()
    total_new = 0
    ok = failed = 0
    summary_lines = []

    try:
        for t in collections:
            for keyword in keywords:
                for field in SEARCH_FIELDS:
                    label = f"{t}|{keyword}|{field}"
                    label_new = 0
                    try:
                        page = 1
                        while page <= max_pages:
                            url = _api_url(base, t, keyword, field, page, page_size)
                            resp = session.get(url, timeout=cfg["settings"]["timeout"],
                                               headers={"User-Agent": "Mozilla/5.0",
                                                        "Referer": "https://sousuo.www.gov.cn/sousuo/search.shtml"})
                            resp.raise_for_status()
                            data = resp.json()
                            vo = (data or {}).get("searchVO") or {}
                            records = vo.get("listVO") or []
                            if not records:
                                break
                            for rec in records:
                                item = _parse_record(rec, t)
                                # 无日期或早于窗口的跳过（政策库为正式文件，必须有日期）
                                if not item["published_date"]:
                                    continue
                                from datetime import datetime
                                if datetime.strptime(item["published_date"], "%Y-%m-%d") < start_dt:
                                    continue
                                if store.insert_policy(item, kw_cfg) == "new":
                                    total_new += 1
                                    label_new += 1
                            total_pages = int(vo.get("totalpage") or 1)
                            if page >= total_pages:
                                break
                            page += 1
                            time.sleep(random.uniform(1.0, 2.0))
                        ok += 1
                        summary_lines.append(f"  ✅ {label}: 新增 {label_new} 条")
                        print(f"  ✅ {label}: 新增 {label_new} 条")
                        store.record_health(label, base, "ok", "", 0, label_new)
                    except Exception as e:  # noqa: BLE001
                        failed += 1
                        detail = f"{type(e).__name__}: {e}"
                        summary_lines.append(f"  ❌ {label}: {detail}")
                        print(f"  ❌ {label}: {detail}")
                        store.record_health(label, base, "failed", detail)
                    time.sleep(random.uniform(1.0, 2.0))
    finally:
        session.close()

    return {"ok": ok, "failed": failed, "new": total_new, "results": summary_lines}
