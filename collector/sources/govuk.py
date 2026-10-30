# -*- coding: utf-8 -*-
"""英国 gov.uk 政策检索（官方搜索 Atom + 组织级 Atom，需代理）。

为何不用 documents.atom / policies.atom：
- `.../department-for-science-innovation-and-technology.atom` 直接可用，但只有"最新 20 条"、
  且不再按主题过滤；
- `.../policies.atom` 在 2026-10 实测返回 **406**（gov.uk 已不输出该端点）；
- `https://www.gov.uk/api/search.json` 能用，但连续请求后会稳定返回 **422**（限流页），
  不适合做 100+ 页的回填。

最终采用 **搜索结果 Atom**，它支持关键词 + 部门过滤 + `page` 翻页，实测可回溯到 2023 年：
    https://www.gov.uk/search/all.atom?keywords=artificial+intelligence
        &organisations[]=department-for-science-innovation-and-technology&page=N
每页 10 条，按日期倒序；翻到早于研究窗口或空页即停。
"""

from datetime import datetime

import feedparser

from .. import store
from ..config import load_config
from ..fetcher import get_session, http_get
from ..normalize import fmt_date, parse_date_flexible, strip_html

SOURCE_NAME = "英国政府网站(gov.uk)"
ATOM = "https://www.gov.uk/search/all.atom"
NEWS_ATOM = "https://www.gov.uk/search/news-and-communications.atom"
PAGE_DELAY = 1.0

# 部门 slug → 中文机构名
ORG_ZH = {
    "department-for-science-innovation-and-technology": "英国科学、创新与技术部(DSIT)",
    "department-for-business-energy-and-industrial-strategy": "英国商业、能源与产业战略部(BEIS)",
    "department-for-culture-media-and-sport": "英国文化、媒体与体育部(DCMS)",
    "department-for-digital-culture-media-sport": "英国数字、文化、媒体与体育部(DCMS)",
    "office-for-artificial-intelligence": "英国人工智能办公室(OAI)",
}

# 标题前缀（gov.uk 用 "Policy paper: xxx" 表示文档类型）→ 中文类型
PREFIX_ZH = {
    "policy paper": "政策文件",
    "publication": "出版物",
    "press release": "新闻稿",
    "news story": "新闻",
    "guidance": "指南",
    "detailed guide": "详细指南",
    "consultation": "征求意见稿",
    "consultation outcome": "征求意见结果",
    "impact assessment": "影响评估",
    "research": "研究报告",
    "statistics": "统计",
    "official statistics": "官方统计",
    "national statistics": "国家统计",
    "transparency": "透明度数据",
    "corporate report": "机构报告",
    "speech": "演讲",
    "written statement": "书面声明",
    "statement to parliament": "议会声明",
    "notice": "公告",
    "decision": "决定",
    "document collection": "文件集",
    "case study": "案例",
    "html publication": "政策出版物",
    "independent report": "独立报告",
}


def _split_title(raw_title: str) -> tuple:
    """从 "Policy paper: 标题" 里拆出 (类型中文, 纯标题)。"""
    t = strip_html(raw_title or "")
    if ":" in t:
        head, _, tail = t.partition(":")
        zh = PREFIX_ZH.get(head.strip().lower())
        if zh and tail.strip():
            return zh, tail.strip()
    return "", t


def _parse(entry, org: str) -> dict:
    title_type, title = _split_title(entry.get("title", ""))
    link = str(entry.get("link") or "").strip()
    if not title or not link:
        return {}
    dt = parse_date_flexible(str(entry.get("updated") or entry.get("published") or ""))
    return {
        "title": title[:300],
        "url": link,
        "summary": strip_html(str(entry.get("summary") or ""), limit=2000),
        "jurisdiction": "UK",
        "jurisdiction_name": "英国",
        "level": "中央",
        "issuing_body": ORG_ZH.get(org, "英国政府"),
        "policy_type": title_type or "政策文件",
        "language": "en",
        "published_date": fmt_date(dt),
        "source_name": SOURCE_NAME,
        "collection_method": "govuk_atom",
        "extra": {"govuk_org": org},
    }


def _fetch_pages(session, atom_url: str, org: str, keyword: str,
                 max_pages: int, start_dt: datetime) -> list:
    """翻页直到：空页 / 早于时间窗 / 达到页数上限。返回原始 entry 列表。"""
    import time
    out, page = [], 1
    while page <= max_pages:
        params = {"keywords": keyword, "organisations[]": org, "page": page}
        resp = http_get(atom_url, session=session, params=params)
        feed = feedparser.parse(resp.content)
        entries = feed.entries or []
        if not entries:
            break
        oldest = None
        for e in entries:
            out.append(e)
            dt = parse_date_flexible(str(e.get("updated") or e.get("published") or ""))
            if dt and (oldest is None or dt < oldest):
                oldest = dt
        if oldest and oldest < start_dt:
            break
        page += 1
        time.sleep(PAGE_DELAY)
    return out


def run(max_pages: int = 120) -> dict:
    cfg = load_config()
    g = cfg.get("govuk_api") or {}
    if not g.get("enabled", True):
        return {"ok": 0, "failed": 0, "new": 0, "results": []}

    start_dt = cfg["settings"]["start_date_dt"]
    kw_cfg = cfg["_keywords"]
    orgs = g.get("organisations") or []
    keywords = g.get("keywords") or ["artificial intelligence"]
    max_pages = int(g.get("max_pages") or max_pages)

    session = get_session(force_proxy=True)

    records, seen = [], set()
    raw_count = 0
    for org in orgs:
        for kw in keywords:
            try:
                entries = _fetch_pages(session, ATOM, org, kw, max_pages, start_dt)
            except Exception as e:  # noqa: BLE001
                msg = f"{org} / {kw}: {type(e).__name__}: {str(e)[:90]}"
                print(f"    ⚠️ {msg}")
                continue
            raw_count += len(entries)
            got = 0
            for e in entries:
                rec = _parse(e, org)
                if not rec or rec["url"] in seen:
                    continue
                seen.add(rec["url"])
                records.append(rec)
                got += 1
            print(f"    {ORG_ZH.get(org, org)} · “{kw}” → {got} 条（翻 {max_pages} 页内）")

    total_new = skipped_date = skipped_offtopic = 0
    for rec in records:
        if rec["published_date"] and datetime.strptime(rec["published_date"], "%Y-%m-%d") < start_dt:
            skipped_date += 1
            continue
        # gov.uk 的搜索是模糊匹配，实测 80% 命中与 AI 无关（半导体、数字就业统计等）。
        # 这里用与入库相同的关键词规则做二次过滤：标题+摘要里一个 AI 词都没有的，直接丢弃。
        from ..normalize import tag_ai
        if not tag_ai(f"{rec['title']} {rec['summary']}", kw_cfg):
            skipped_offtopic += 1
            continue
        if store.insert_policy(rec, kw_cfg) == "new":
            total_new += 1

    msg = (f"  ✅ gov.uk: 抓取 {raw_count} 条 / 去重后 {len(records)} 条，入库 {total_new} 条"
           f"（早于窗口 {skipped_date} / 非 AI 主题 {skipped_offtopic}）")
    print(msg)
    store.record_health(SOURCE_NAME, ATOM, "ok", f"去重后{len(records)}条", raw_count, total_new)
    return {"ok": 1, "failed": 0, "new": total_new, "results": [msg]}
