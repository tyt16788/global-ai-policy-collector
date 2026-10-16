# -*- coding: utf-8 -*-
"""OECD.AI 政策观察站回填（70+ 国家 1000+ 条 AI 政策举措，需代理）。

端点为站点内部 JSON 接口（未正式文档化）。若失效：
浏览器打开 https://oecd.ai/en/dashboards/policy-initiatives → F12 Network →
找返回政策列表 JSON 的请求 → 把地址更新到 config.yaml 的 oecd.endpoint。
解析器对常见字段名做了容错映射，返回结构变化时大概率仍能工作。
"""

from ..config import load_config, get_proxy
from ..fetcher import http_get
from ..normalize import parse_date_flexible, fmt_date, guess_language, strip_html
from .. import store

# OECD 国家/地区名 → (代码, 中文名, 层级)
JURISDICTION_MAP = {
    "China": ("CN", "中国", "中央"),
    "Chinese Taipei": ("TW", "中国台湾", "地方"),
    "United States": ("US", "美国", "中央"),
    "European Union": ("EU", "欧盟", "超国家"),
    "United Kingdom": ("UK", "英国", "中央"),
    "Japan": ("JP", "日本", "中央"),
    "Korea": ("KR", "韩国", "中央"),
    "Germany": ("DE", "德国", "中央"),
    "France": ("FR", "法国", "中央"),
    "Singapore": ("SG", "新加坡", "中央"),
    "Canada": ("CA", "加拿大", "中央"),
}


def _find_initiative_list(payload) -> list:
    """在返回 JSON 的常见位置找政策列表。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("policyInitiatives", "initiatives", "policies", "results", "data"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            nested = _find_initiative_list(v)
            if nested:
                return nested
    return []


def _first(item: dict, *keys):
    for k in keys:
        if k in item and item[k] not in (None, ""):
            return item[k]
    return ""


def _parse_item(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    title = strip_html(str(_first(item, "title", "name", "initiativeName")))
    if not title:
        return {}
    country = str(_first(item, "country", "publicFieldName", "jurisdiction", "economy"))
    date_raw = _first(item, "creationDate", "startDate", "date", "publicationDate", "issued")
    pub_dt = parse_date_flexible(str(date_raw)) if date_raw else None

    code, zh_name, level = JURISDICTION_MAP.get(country, ("", country, "中央"))
    ptype_raw = _first(item, "initiativeType", "policyType", "type")
    # OECD 类型常为 "Legally binding" / "Non-binding" 等
    ptype_map = {"Legally binding": "法规(有约束力)", "Non-binding": "指南/战略(无约束力)"}
    policy_type = ptype_map.get(str(ptype_raw), str(ptype_raw) or "政策举措")

    return {
        "title": title,
        "url": str(_first(item, "url", "link", "website", "sourceUrl")),
        "summary": strip_html(str(_first(item, "description", "summary", "abstract")), limit=2000),
        "jurisdiction": code,
        "jurisdiction_name": zh_name,
        "level": level,
        "issuing_body": str(_first(item, "issuingBody", "organisation", "organization", "responsibleBody")) or "OECD.AI",
        "policy_type": policy_type,
        "language": guess_language(title, default="en"),
        "published_date": fmt_date(pub_dt),
        "source_name": "OECD.AI政策观察站",
        "collection_method": "oecd_api",
        "extra": {
            "oecd_id": _first(item, "id", "initiativeId"),
            "country_raw": country,
            "categories": _first(item, "categories", "tags", "sectors"),
        },
    }


def run() -> dict:
    cfg = load_config()
    o = cfg.get("oecd") or {}
    if not o.get("enabled", True):
        return {"ok": 0, "failed": 0, "new": 0, "results": []}

    start_dt = cfg["settings"]["start_date_dt"]
    kw_cfg = cfg["_keywords"]
    wanted = set(o.get("jurisdictions") or [])

    try:
        resp = http_get(o["endpoint"])
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        hint = "" if get_proxy() else "（OECD 需代理：请开 Clash 后在 config.yaml settings.proxy 填写或设置 HTTPS_PROXY）"
        print(f"  ❌ OECD.AI: {detail}{hint}")
        store.record_health("OECD.AI政策观察站", o["endpoint"], "failed", detail + hint)
        return {"ok": 0, "failed": 1, "new": 0, "results": [f"  ❌ {detail}{hint}"]}

    items = _find_initiative_list(payload)
    total_new = 0
    skipped_country = 0
    for item in items:
        rec = _parse_item(item)
        if not rec:
            continue
        country_raw = rec["extra"].get("country_raw", "")
        if wanted and country_raw not in wanted:
            skipped_country += 1
            continue
        # 日期过滤：有日期且早于窗口的丢弃；无日期保留（导出时归"日期待定"）
        if rec["published_date"]:
            from datetime import datetime
            if datetime.strptime(rec["published_date"], "%Y-%m-%d") < start_dt:
                continue
        if store.insert_policy(rec, kw_cfg) == "new":
            total_new += 1

    msg = f"  ✅ OECD.AI: 获取 {len(items)} 条，入库 {total_new} 条（按国家筛选跳过 {skipped_country} 条）"
    print(msg)
    store.record_health("OECD.AI政策观察站", o["endpoint"], "ok", f"原始{len(items)}条", len(items), total_new)
    return {"ok": 1, "failed": 0, "new": total_new, "results": [msg]}
