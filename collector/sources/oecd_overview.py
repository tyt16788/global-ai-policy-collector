# -*- coding: utf-8 -*-
"""为 OECD 记录补全文：优先用 API 自带的 overview / moreInfos 字段。

背景
----
OECD 的部分政策记录没有 `website`（各国官网原文链接缺失），入库时 url 落回
`https://oecd.ai/en/dashboards/policy-initiatives/<slug>` —— 这是 Angular SPA，
静态抓取只能拿到空壳，于是 fulltext 全部标记 `failed:仪表盘页面无静态正文`（303 条）。

但实际上 API 在列表接口里就返回了长篇正文：
- `overview`   平均 2,394 字（含 <p> 标签的官方概述）
- `moreInfos`  平均 1,080 字（补充说明）
- `description` 平均 435 字（短摘要，入库时已进 summary 列）

所以这 303 条**不需要爬虫**，重新拉一遍列表接口、把 overview + moreInfos 拼成
full_text 写回即可。

口径说明（重要）
----------------
这份正文是 **OECD 秘书处编撰的英文概述**，不是该国政府原文。入库时：
- `fulltext_status = "ok:oecd_overview"`（不是纯 "ok"，便于统计时区分）
- `extra_json.text_source = "oecd_overview"` / `"oecd_overview+more_infos"`
- 原文链接（`relevantUrls` / `sourceFiles`）若存在，写进 `extra_json.official_links`
"""

import json

from .. import store
from ..config import load_config
from ..fetcher import get_session, http_get
from ..normalize import strip_html

ENDPOINT = "https://api.oecd.ai/policy-initiatives/public"
STATUS_OVERVIEW = "ok:oecd_overview"
STATUS_DESC_ONLY = "ok:oecd_desc_only"   # 只有 300-500 字短摘兜底，不算真全文
BATCH = 200
PAGE_DELAY = 0.25


def _build_text(item: dict) -> tuple:
    """返回 (正文, 文本来源标记, 官方链接列表)。

    优先级：overview + moreInfos（OECD 长篇概述）→ 只有 description 时用它兜底。
    description 兜底会返回 `desc_only`，调用方据此标不同的状态，不混进"全文"统计。
    """
    overview = strip_html(str(item.get("overview") or "")).strip()
    more = strip_html(str(item.get("moreInfos") or "")).strip()
    desc = strip_html(str(item.get("description") or "")).strip()
    parts = [p for p in (overview, more) if p]
    text = "\n\n".join(parts)

    links = []
    for key in ("relevantUrls", "evaluationUrls"):
        v = item.get(key)
        if isinstance(v, list):
            links.extend([str(x) for x in v if x])
    for key in ("sourceFiles", "relevantFiles"):
        v = item.get(key)
        if isinstance(v, list):
            for f in v:
                if isinstance(f, dict):
                    u = f.get("url") or f.get("file") or f.get("link")
                    if u:
                        links.append(str(u))
    website = item.get("website")
    if website:
        links.insert(0, str(website))

    if overview and more:
        src = "oecd_overview+more_infos"
    elif overview:
        src = "oecd_overview"
    elif more:
        src = "more_infos"
    elif len(desc) >= 200:
        # OECD 库里这篇本来就没写概述，只有 300-500 字短摘；用描述兜底但明确标 downgrade
        text = desc
        src = "desc_only"
    else:
        src = ""
    return text, src, links


def run() -> dict:
    cfg = load_config()
    if not (cfg.get("oecd") or {}).get("enabled", True):
        return {"ok": 0, "failed": 0, "updated": 0}

    # 找出挂着"仪表盘无正文"的 OECD 记录，建立 slug → guid
    df = store.load_master()
    mask = (df["source_name"] == "OECD.AI政策观察站") & (
        df["fulltext_status"].str.contains("仪表盘", na=False))
    targets = df[mask]
    by_slug = {}
    for _, row in targets.iterrows():
        try:
            extra = json.loads(row.get("extra_json") or "{}")
        except json.JSONDecodeError:
            extra = {}
        slug = extra.get("oecd_slug") or ""
        if slug:
            by_slug[slug] = row["guid"]
    print(f"📄 待补正文的 OECD 记录 {len(targets)} 条（按 slug 索引 {len(by_slug)} 条）")
    if not by_slug:
        return {"ok": 0, "failed": 0, "updated": 0}

    session = get_session(force_proxy=True)
    updated = empty = desc_only = 0
    page, last_page = 1, 1
    import time
    while page <= last_page:
        payload = http_get(ENDPOINT, session=session, params={"page": page}).json()
        if page == 1:
            last_page = int(payload.get("lastPage") or 1)
            print(f"    OECD 共 {last_page} 页，开始回填…")
        for item in payload.get("data") or []:
            slug = str(item.get("slug") or "")
            guid = by_slug.get(slug)
            if not guid:
                continue
            text, src, links = _build_text(item)
            if not src:
                empty += 1
                continue
            # 只有短摘兜底的，标成独立状态，不计入"全文已入库"
            status = STATUS_DESC_ONLY if src == "desc_only" else STATUS_OVERVIEW
            store.set_fulltext(guid, text, status)
            # 补一条来源说明，供论文引用时区分"官方概述"与"政府原文"
            store.patch_extra(guid, {"text_source": src, "official_links": links[:5]})
            if src == "desc_only":
                desc_only += 1
            else:
                updated += 1
        page += 1
        time.sleep(PAGE_DELAY)

    store.save()
    print(f"✅ OECD 正文回填：概述 {updated} 条（{STATUS_OVERVIEW}）、"
          f"短摘兜底 {desc_only} 条（{STATUS_DESC_ONLY}）、库里本来没内容 {empty} 条")
    return {"ok": 1, "failed": 0, "updated": updated, "desc_only": desc_only, "empty": empty}
