# -*- coding: utf-8 -*-
"""美国 Federal Register 官方 API 回填（含行政命令、法规、通知，直连可用）。

覆盖美国行政部门全部正式文件，如 EO 14110（AI 行政命令）即在此发布。
文档: https://www.federalregister.gov/developers/documentation/api/v1
"""

import time
import random

from ..config import load_config
from ..fetcher import get_session
from ..normalize import fmt_date, parse_date_flexible
from .. import store

TYPE_MAP = {
    "RULE": "最终法规",
    "PROPOSED RULE": "法规草案",
    "NOTICE": "通知公告",
    "PRESDOCU": "总统文件(行政命令/公告)",
    "PRESIDENTIAL DOCU": "总统文件(行政命令/公告)",
    "CORRECTION": "更正",
}

FIELDS = ["title", "html_url", "pdf_url", "raw_text_url", "publication_date", "type",
          "agencies", "abstract", "document_number"]


def run() -> dict:
    cfg = load_config()
    fr = cfg.get("federal_register") or {}
    if not fr.get("enabled", True):
        return {"ok": 0, "failed": 0, "new": 0, "results": []}

    base = fr["base"]
    terms = fr.get("terms", [])
    per_page = int(fr.get("per_page", 500))
    max_pages = int(fr.get("max_pages", 40))
    since = cfg["settings"]["start_date"]
    kw_cfg = cfg["_keywords"]

    session = get_session()
    total_new = 0
    results = []
    try:
        for term in terms:
            term_new = 0
            try:
                page = 1
                while page <= max_pages:
                    params = {
                        "conditions[term]": term,
                        "conditions[publication_date][gte]": since,
                        "per_page": per_page,
                        "order": "newest",
                        "page": page,
                        "fields[]": FIELDS,
                    }
                    resp = session.get(base, params=params,
                                       timeout=cfg["settings"]["timeout"],
                                       headers={"User-Agent": "policy-collector/5.0 (academic research)"})
                    resp.raise_for_status()
                    data = resp.json()
                    docs = data.get("results") or []
                    if not docs:
                        break
                    for doc in docs:
                        pub_dt = parse_date_flexible(doc.get("publication_date", ""))
                        agencies = ", ".join(a.get("name", "") for a in (doc.get("agencies") or []) if isinstance(a, dict))
                        rec = {
                            "title": doc.get("title", "").strip(),
                            "url": doc.get("html_url", ""),
                            "summary": (doc.get("abstract") or "")[:2000],
                            "jurisdiction": "US",
                            "jurisdiction_name": "美国",
                            "level": "中央",
                            "issuing_body": agencies or "Federal Register",
                            "policy_type": TYPE_MAP.get((doc.get("type") or "").upper(), doc.get("type") or "其他"),
                            "language": "en",
                            "published_date": fmt_date(pub_dt),
                            "source_name": "Federal Register",
                            "collection_method": "federal_register_api",
                            "extra": {
                                "document_number": doc.get("document_number", ""),
                                "pdf_url": doc.get("pdf_url", ""),
                                "raw_text_url": doc.get("raw_text_url", ""),
                            },
                        }
                        if not rec["title"]:
                            continue
                        if store.insert_policy(rec, kw_cfg) == "new":
                            total_new += 1
                            term_new += 1
                    total_pages = int(data.get("total_pages") or 1)
                    if page >= total_pages:
                        break
                    page += 1
                    time.sleep(random.uniform(1.0, 2.0))
                msg = f"  ✅ Federal Register[{term}]: 新增 {term_new} 条"
                print(msg)
                results.append(msg)
                store.record_health(f"Federal Register[{term}]", base, "ok", "", 0, term_new)
            except Exception as e:  # noqa: BLE001
                detail = f"{type(e).__name__}: {e}"
                msg = f"  ❌ Federal Register[{term}]: {detail}"
                print(msg)
                results.append(msg)
                store.record_health(f"Federal Register[{term}]", base, "failed", detail)
            time.sleep(random.uniform(1.0, 2.0))
    finally:
        session.close()

    return {"ok": len([r for r in results if "✅" in r]), "failed": len([r for r in results if "❌" in r]),
            "new": total_new, "results": results}
