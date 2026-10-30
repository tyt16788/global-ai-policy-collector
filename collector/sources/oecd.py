# -*- coding: utf-8 -*-
"""OECD.AI 政策观察站回填（70+ 国家 2,500+ 条 AI 政策举措，需代理）。

真实端点（2026-10 实测）：https://api.oecd.ai/policy-initiatives/public?page=N
返回 {data:[...], currentPage, lastPage, perPage, total}，每页 20 条。

字段要点：
- englishName / originalName → 标题
- startYear（只有年份）→ published_date = "YYYY-01-01"，并在 extra 标记 year_only
- gaiinCountry.name → 国家，website → 原文地址
- initiativeType.name / category / extentBinding → 政策类型
"""

from datetime import datetime

from .. import store
from ..config import load_config
from ..fetcher import get_session, http_get
from ..normalize import fmt_date, guess_language, parse_date_flexible, strip_html

SOURCE_NAME = "OECD.AI政策观察站"
ENDPOINT = "https://api.oecd.ai/policy-initiatives/public"
MAX_PAGES = 200          # 兜底，正常由接口返回的 lastPage 决定
PAGE_DELAY = 0.25        # 每页之间停顿，避免给 OECD 服务器压力

# OECD 国家名 → (代码, 中文名, 层级)
JURISDICTION_MAP = {
    "China": ("CN", "中国", "中央"),
    "Chinese Taipei": ("TW", "中国台湾", "地方"),
    "Hong Kong, China": ("HK", "中国香港", "地方"),
    "United States": ("US", "美国", "中央"),
    "European Union": ("EU", "欧盟", "超国家"),
    "United Kingdom": ("UK", "英国", "中央"),
    "Japan": ("JP", "日本", "中央"),
    "Korea": ("KR", "韩国", "中央"),
    "Germany": ("DE", "德国", "中央"),
    "France": ("FR", "法国", "中央"),
    "Singapore": ("SG", "新加坡", "中央"),
    "Canada": ("CA", "加拿大", "中央"),
    "Australia": ("AU", "澳大利亚", "中央"),
    "India": ("IN", "印度", "中央"),
    "Brazil": ("BR", "巴西", "中央"),
    "Israel": ("IL", "以色列", "中央"),
    "Italy": ("IT", "意大利", "中央"),
    "Spain": ("ES", "西班牙", "中央"),
    "Netherlands": ("NL", "荷兰", "中央"),
    "Sweden": ("SE", "瑞典", "中央"),
    "Norway": ("NO", "挪威", "中央"),
    "Denmark": ("DK", "丹麦", "中央"),
    "Finland": ("FI", "芬兰", "中央"),
    "Switzerland": ("CH", "瑞士", "中央"),
    "Austria": ("AT", "奥地利", "中央"),
    "Belgium": ("BE", "比利时", "中央"),
    "Ireland": ("IE", "爱尔兰", "中央"),
    "Poland": ("PL", "波兰", "中央"),
    "Portugal": ("PT", "葡萄牙", "中央"),
    "Czech Republic": ("CZ", "捷克", "中央"),
    "Czechia": ("CZ", "捷克", "中央"),
    "Greece": ("GR", "希腊", "中央"),
    "Turkey": ("TR", "土耳其", "中央"),
    "Türkiye": ("TR", "土耳其", "中央"),
    "Russia": ("RU", "俄罗斯", "中央"),
    "Mexico": ("MX", "墨西哥", "中央"),
    "Argentina": ("AR", "阿根廷", "中央"),
    "Chile": ("CL", "智利", "中央"),
    "Colombia": ("CO", "哥伦比亚", "中央"),
    "New Zealand": ("NZ", "新西兰", "中央"),
    "South Africa": ("ZA", "南非", "中央"),
    "Saudi Arabia": ("SA", "沙特阿拉伯", "中央"),
    "United Arab Emirates": ("AE", "阿联酋", "中央"),
    "Egypt": ("EG", "埃及", "中央"),
    "Indonesia": ("ID", "印度尼西亚", "中央"),
    "Malaysia": ("MY", "马来西亚", "中央"),
    "Thailand": ("TH", "泰国", "中央"),
    "Vietnam": ("VN", "越南", "中央"),
    "Philippines": ("PH", "菲律宾", "中央"),
    "Ukraine": ("UA", "乌克兰", "中央"),
    "Romania": ("RO", "罗马尼亚", "中央"),
    "Hungary": ("HU", "匈牙利", "中央"),
    "Luxembourg": ("LU", "卢森堡", "中央"),
    "Iceland": ("IS", "冰岛", "中央"),
    "Lithuania": ("LT", "立陶宛", "中央"),
    "Estonia": ("EE", "爱沙尼亚", "中央"),
    "Latvia": ("LV", "拉脱维亚", "中央"),
    "Slovenia": ("SI", "斯洛文尼亚", "中央"),
    "Slovakia": ("SK", "斯洛伐克", "中央"),
    "Croatia": ("HR", "克罗地亚", "中央"),
    "Bulgaria": ("BG", "保加利亚", "中央"),
    "Serbia": ("RS", "塞尔维亚", "中央"),
    "Qatar": ("QA", "卡塔尔", "中央"),
    "Morocco": ("MA", "摩洛哥", "中央"),
    "Nigeria": ("NG", "尼日利亚", "中央"),
    "Kenya": ("KE", "肯尼亚", "中央"),
    "Peru": ("PE", "秘鲁", "中央"),
    "Costa Rica": ("CR", "哥斯达黎加", "中央"),
    "Uruguay": ("UY", "乌拉圭", "中央"),
    "Pakistan": ("PK", "巴基斯坦", "中央"),
    "Bangladesh": ("BD", "孟加拉国", "中央"),
    "Sri Lanka": ("LK", "斯里兰卡", "中央"),
    "Kazakhstan": ("KZ", "哈萨克斯坦", "中央"),
    "Georgia": ("GE", "格鲁吉亚", "中央"),
    "Armenia": ("AM", "亚美尼亚", "中央"),
    "Azerbaijan": ("AZ", "阿塞拜疆", "中央"),
    "Moldova": ("MD", "摩尔多瓦", "中央"),
    "Cyprus": ("CY", "塞浦路斯", "中央"),
    "Malta": ("MT", "马耳他", "中央"),
    "Bahrain": ("BH", "巴林", "中央"),
    "Jordan": ("JO", "约旦", "中央"),
    "Tunisia": ("TN", "突尼斯", "中央"),
    "Senegal": ("SN", "塞内加尔", "中央"),
    "Rwanda": ("RW", "卢旺达", "中央"),
    "Ghana": ("GH", "加纳", "中央"),
    "Jamaica": ("JM", "牙买加", "中央"),
    "Panama": ("PA", "巴拿马", "中央"),
    "Ecuador": ("EC", "厄瓜多尔", "中央"),
    "Bolivia": ("BO", "玻利维亚", "中央"),
    "Paraguay": ("PY", "巴拉圭", "中央"),
    "Dominican Republic": ("DO", "多米尼加", "中央"),
}

TYPE_ZH = {
    "Legally binding": "法规(有约束力)",
    "Non-binding": "指南/战略(无约束力)",
    "Binding": "法规(有约束力)",
}

# initiativeType 英文名片段 → 中文大类
INITIATIVE_TYPE_ZH = [
    ("Law/legislation/act", "法律法规"),
    ("Regulation", "法规"),
    ("Guideline", "指南"),
    ("Strategy", "战略"),
    ("Action plan", "行动计划"),
    ("Policy paper", "政策文件"),
    ("Standard", "标准"),
    ("Initiative", "倡议"),
    ("Programme", "计划"),
    ("Program", "计划"),
    ("Executive order", "行政命令"),
    ("Funding", "资助"),
    ("Sandbox", "监管沙盒"),
    ("Assessment", "评估"),
]


def _find_initiative_list(payload) -> list:
    """在返回 JSON 的常见位置找政策列表（兼容字段改名）。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "policyInitiatives", "initiatives", "policies", "results"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    return []


def _first(item: dict, *keys):
    for k in keys:
        if k in item and item[k] not in (None, "", []):
            return item[k]
    return ""


def _type_zh(item: dict) -> str:
    itype = item.get("initiativeType")
    name = str(itype.get("name") or "") if isinstance(itype, dict) else str(itype or "")
    for needle, zh in INITIATIVE_TYPE_ZH:
        if needle.lower() in name.lower():
            return zh
    category = str(item.get("category") or "")
    for needle, zh in INITIATIVE_TYPE_ZH:
        if needle.lower() in category.lower():
            return zh
    return TYPE_ZH.get(category, category or "政策举措")


def _country_of(item: dict):
    """取国家名、三字母代码：优先 gaiinCountry 对象。"""
    c = item.get("gaiinCountry")
    if isinstance(c, dict):
        return str(c.get("name") or ""), str(c.get("code") or "")
    if isinstance(c, str):
        return c, ""
    return "", ""


def _parse_item(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    title = strip_html(str(_first(item, "englishName", "originalName", "title", "name")))
    if not title:
        return {}
    country, code3 = _country_of(item)
    code, zh_name, level = JURISDICTION_MAP.get(country, ("", country or "全球", "中央"))

    pub_str, year_only = "", False
    try:
        pub_str = f"{int(item.get('startYear'))}-01-01"
        year_only = True
    except (TypeError, ValueError):
        dt = parse_date_flexible(str(_first(item, "startDate", "date", "issued", "publishedDate")))
        pub_str = fmt_date(dt)

    tags = item.get("tags")
    tag_names = [str(t.get("name")) for t in tags if isinstance(t, dict)] if isinstance(tags, list) else []
    website = str(_first(item, "website", "url", "link", "sourceUrl"))
    slug = str(item.get("slug") or "")
    url = website or (f"https://oecd.ai/en/dashboards/policy-initiatives/{slug}" if slug else "")

    return {
        "title": title[:300],
        "url": url,
        "summary": strip_html(str(item.get("description") or ""), limit=2000),
        "jurisdiction": code,
        "jurisdiction_name": zh_name,
        "level": level,
        "issuing_body": str(_first(item, "responsibleOrganisation", "issuingBody", "organisation")) or "OECD.AI 收录",
        "policy_type": _type_zh(item),
        "language": guess_language(title, default="en"),
        "published_date": pub_str,
        "source_name": SOURCE_NAME,
        "collection_method": "oecd_api",
        "extra": {
            "oecd_id": item.get("id"),
            "oecd_slug": slug,
            "country_raw": country,
            "country_code3": code3,
            "status": item.get("status"),
            "category": item.get("category"),
            "extent_binding": item.get("extentBinding"),
            "initiative_type": (item.get("initiativeType") or {}).get("name")
            if isinstance(item.get("initiativeType"), dict) else item.get("initiativeType"),
            "tags": tag_names[:10],
            "year_only": year_only,
            "oecd_updated_at": item.get("updatedAt"),
        },
    }


def fetch_all(session=None, max_pages: int = MAX_PAGES) -> list:
    """逐页拉取全部政策举措（page 从 1 开始）。"""
    import time
    session = session or get_session(force_proxy=True)
    items, page, last_page = [], 1, 1
    while page <= min(last_page, max_pages):
        payload = http_get(f"{ENDPOINT}?page={page}", session=session).json()
        chunk = _find_initiative_list(payload)
        items.extend(chunk)
        if page == 1:
            try:
                last_page = int(payload.get("lastPage") or 1)
                print(f"    OECD 共 {payload.get('total')} 条 / {last_page} 页（每页 {len(chunk)} 条）")
            except (TypeError, ValueError):
                pass
        page += 1
        time.sleep(PAGE_DELAY)
    return items


def run(max_pages: int = MAX_PAGES) -> dict:
    cfg = load_config()
    o = cfg.get("oecd") or {}
    if not o.get("enabled", True):
        return {"ok": 0, "failed": 0, "new": 0, "results": []}

    start_dt = cfg["settings"]["start_date_dt"]
    kw_cfg = cfg["_keywords"]
    wanted = set(o.get("jurisdictions") or [])

    try:
        items = fetch_all(max_pages=max_pages)
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        print(f"  ❌ OECD.AI: {detail}（OECD 需代理：settings.proxy 或 HTTPS_PROXY）")
        store.record_health(SOURCE_NAME, ENDPOINT, "failed", detail)
        return {"ok": 0, "failed": 1, "new": 0, "results": [f"  ❌ {detail}"]}

    total_new = skipped_country = skipped_date = skipped_nourl = 0
    for item in items:
        rec = _parse_item(item)
        if not rec:
            continue
        if not rec["url"]:
            skipped_nourl += 1
            continue
        if wanted and rec["extra"].get("country_raw", "") not in wanted:
            skipped_country += 1
            continue
        if rec["published_date"] and datetime.strptime(rec["published_date"], "%Y-%m-%d") < start_dt:
            skipped_date += 1
            continue
        if store.insert_policy(rec, kw_cfg) == "new":
            total_new += 1

    msg = (f"  ✅ OECD.AI: 拉取 {len(items)} 条，入库 {total_new} 条"
           f"（国家外 {skipped_country} / 早于窗口 {skipped_date} / 无原文链接 {skipped_nourl}）")
    print(msg)
    store.record_health(SOURCE_NAME, ENDPOINT, "ok", f"原始{len(items)}条", len(items), total_new)
    return {"ok": 1, "failed": 0, "new": total_new, "results": [msg]}
