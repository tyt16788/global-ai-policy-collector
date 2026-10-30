# -*- coding: utf-8 -*-
"""六大科技巨头官方博客采集（信号层）。

与政策层的区别
--------------
- 只写 `data/signal_master.csv`，不碰政策主表（口径见 signal_store.py 顶部说明）。
- 每条带 `company` / `signal_layer`(governance|tech) / `topic_tags`。
- **全文是硬性入库标准**：RSS 只用来发现文章和取摘要，正文必须另抓；
  抓不到正文的记录 `fulltext_status=failed:*`，不算"可用记录"，每轮自动重试。

两种采集模式
------------
- `rss` ：OpenAI / Google DeepMind / NVIDIA（Meta、Microsoft 默认挂起，需代理）
- `html`：Anthropic（官方没有 RSS，解析 /news 列表页）

命令行
------
    python -m collector blog                 # 只采集元数据 + 摘要（不抓正文）
    python -m collector blog --fulltext      # 采集后立刻补全文（推荐）
    python -m collector blog --only OpenAI --fulltext --limit 50   # 调试单家
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

from .. import signal_store
from ..config import load_config, get_proxy
from ..fetcher import get_session
from ..normalize import fmt_date, strip_html
from .rss_source import _entry_date, _entry_summary

# 每域名最小请求间隔（秒）：企业博客反爬比政府站更敏感，宁可慢
DOMAIN_MIN_INTERVAL = 1.0
_last_req: dict = {}
_req_lock = threading.Lock()

# ---------------- 分层与主题打标 ----------------
# 治理/政策侧：企业"对规则的主张"
GOVERNANCE_WORDS = [
    "policy", "policies", "regulation", "regulator", "regulatory", "legislation", "law",
    "legal", "governance", "safety", "security", "privacy", "ethics", "ethic", "risk",
    "compliance", "standard", "framework", "guideline", "transparency", "copyright",
    "liability", "court", "lawsuit", "ban", "moratorium", "executive order", "treaty",
    "government", "congress", "senate", "parliament", "ministry", "public sector",
    "procurement", "defense", "national security", "election", "misuse", "deepfake",
    "child safety", "content moderation", "watermark", "provenance", "audit",
]
# 技术/产品侧：企业"做了什么"
TECH_WORDS = [
    "model", "models", "release", "released", "launch", "launched", "preview", "api",
    "gpt", "llm", "agent", "agents", "benchmark", "training", "inference", "fine-tune",
    "fine-tuning", "dataset", "multimodal", "reasoning", "context window", "latency",
    "throughput", "gpu", "chip", "supercomputer", "data center", "datacenter",
    "open source", "open-source", "developer", "product", "update", "version",
    "research", "paper", "study", "breakthrough", "sota", "state-of-the-art",
]

TOPIC_RULES = [
    ("政策法规", ["policy", "regulation", "legislation", "law", "legal", "compliance",
                 "standard", "framework", "guideline", "copyright", "liability"]),
    ("安全治理", ["safety", "security", "privacy", "ethics", "risk", "misuse", "deepfake",
                 "child safety", "content moderation", "watermark", "provenance", "audit"]),
    ("政府合作", ["government", "public sector", "ministry", "mou", "memorandum",
                 "congress", "senate", "parliament", "procurement", "defense", "election"]),
    ("产品发布", ["release", "released", "launch", "launched", "preview", "available",
                 "product", "update", "version", "api"]),
    ("模型能力", ["model", "gpt", "llm", "reasoning", "context window", "multimodal",
                 "benchmark", "sota", "state-of-the-art", "fine-tune"]),
    ("芯片算力", ["gpu", "chip", "supercomputer", "data center", "datacenter", "blackwell",
                 "cuda", "inference", "throughput", "latency", "compute"]),
    ("科学研究", ["research", "paper", "study", "breakthrough", "science", "scientist"]),
    ("行业应用", ["customer", "enterprise", "case study", "healthcare", "finance", "bank",
                 "hospital", "workflow", "deploy", "adoption"]),
    ("合作生态", ["partner", "partnership", "collaborat", "ecosystem", "alliance", "invest"]),
    ("公司动态", ["funding", "valuation", "hiring", "office", "anniversary", "acquisition",
                 "board", "leadership", "ceo"]),
]


def classify(text: str, title: str = "") -> tuple:
    """返回 (signal_layer, topic_tags)。按关键词打分，治理分高算 governance，否则 tech。

    标题里的治理词额外记一次：标题是作者自己的定性（"Usage Policy update"），
    比正文里顺带提到的 "policy" 更能说明这篇文章的性质。
    """
    low = (text or "").lower()
    g = sum(1 for w in GOVERNANCE_WORDS if w in low)
    t = sum(1 for w in TECH_WORDS if w in low)
    low_title = (title or "").lower()
    if low_title:
        g += sum(1 for w in GOVERNANCE_WORDS if w in low_title)
    layer = "governance" if g > t else "tech"
    tags = [name for name, words in TOPIC_RULES if any(w in low for w in words)]
    if not tags:
        tags = ["其他"]
    # 治理类文章若同时命中大量技术词，补一个交叉标签，避免被读成纯产品稿
    if layer == "governance" and "产品发布" in tags:
        tags.append("治理-产品交叉")
    return layer, ", ".join(tags[:5])


def _looks_ai(text: str) -> bool:
    low = (text or "").lower()
    words = ["artificial intelligence", " ai ", "ai-", "-ai", "ai,", "ai.", "llm",
             "generative", "neural", "machine learning", "deep learning", "gpu", "cuda",
             "blackwell", "omniverse", "inference", "chatbot", "agentic", "transformer",
             "copilot", "model", "datacenter", "data center", "accelerated computing"]
    return any(w in low for w in words)


def _throttle(host: str, min_interval: float = None):
    """同域名最小间隔串行控制。OpenAI 这类对高频请求会返回 403 挑战，可按源调慢。"""
    interval = DOMAIN_MIN_INTERVAL if min_interval is None else float(min_interval)
    with _req_lock:
        wait = interval - (time.monotonic() - _last_req.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_req[host] = time.monotonic()


def _session_for(conf: dict):
    """按源决定是否走代理：needs_proxy 的源必须走代理，其余强制直连。

    这是"只在采需代理的站时才开代理"的落地实现——避免国内源被一起带出去。
    """
    return get_session(force_proxy=bool(conf.get("needs_proxy")))


def _get(url: str, conf: dict, timeout: int = 30) -> requests.Response:
    _throttle(urlsplit(url).netloc.lower(), conf.get("rate_limit"))
    session = _session_for(conf)
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp
    finally:
        session.close()


# ---------------- RSS 模式 ----------------
def _collect_rss(conf: dict, start_dt, keywords_cfg: dict) -> dict:
    name = conf["name"]
    company = conf.get("company") or name
    out = {"source": name, "status": "ok", "entries_seen": 0, "new": 0, "skipped": 0, "detail": ""}
    try:
        resp = _get(conf["feed_url"], conf)
        feed = feedparser.parse(resp.content)
        out["entries_seen"] = len(feed.entries or [])
        for entry in feed.entries or []:
            title = strip_html(entry.get("title", "")).strip()
            url = (entry.get("link") or "").strip()
            if not title or not url:
                continue
            pub_dt, _raw = _entry_date(entry)
            if pub_dt and pub_dt < start_dt:
                continue
            summary = _entry_summary(entry)
            if conf.get("ai_filter") and not _looks_ai(f"{title} {summary}"):
                out["skipped"] += 1
                continue
            layer, tags = classify(f"{title} {summary}", title)
            rec = {
                "title": title,
                "url": url,
                "summary": summary,
                "jurisdiction": "GLOBAL",
                "jurisdiction_name": "全球（企业信号）",
                "level": "企业",
                "issuing_body": company,
                "policy_type": "企业博客",
                "language": conf.get("language") or "en",
                "published_date": fmt_date(pub_dt),
                "source_name": name,
                "collection_method": f"company_{conf.get('mode', 'rss')}",
                "source_type": "corporate_blog",
                "signal_layer": layer,
                "company": company,
                "topic_tags": tags,
                "extra": {"blog": conf.get("blog_url", ""), "feed": conf["feed_url"]},
            }
            if signal_store.insert(rec, keywords_cfg) == "new":
                out["new"] += 1
        return out
    except Exception as e:  # noqa: BLE001 — 单源失败不影响其他源
        out["status"] = "failed"
        out["detail"] = f"{type(e).__name__}: {e}"
        if conf.get("needs_proxy") and not get_proxy():
            out["detail"] += "（该源需代理：请在 settings.proxy 填写或设置 HTTPS_PROXY 后重试）"
        return out


# ---------------- HTML 列表页模式（Anthropic） ----------------
_MONTH_RE = re.compile(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4})", re.I)
# 列表卡片常把栏目名拼在标题前（"Announcements An update on ..."），去掉
_TITLE_PREFIX_RE = re.compile(
    r"^(?:Announcements?|Policy|Product|Research|Alignment|Safety|News|Company|Events|"
    r"Engineering|Customers)\s+[:\-–—]?\s*", re.I)


def strip_title_prefix(title: str) -> str:
    return _TITLE_PREFIX_RE.sub("", (title or "").strip()).strip()


def _collect_html(conf: dict, start_dt, keywords_cfg: dict) -> dict:
    name = conf["name"]
    company = conf.get("company") or name
    out = {"source": name, "status": "ok", "entries_seen": 0, "new": 0, "skipped": 0, "detail": ""}
    try:
        resp = _get(conf["list_url"], conf)
        soup = BeautifulSoup(resp.text, "html.parser")
        pattern = re.compile(conf.get("link_pattern", r"/news/[a-z0-9\-]+$"))
        seen = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(conf["list_url"], a["href"]).split("?")[0].rstrip("/")
            if not pattern.search(href) or href in seen:
                continue
            raw_text = a.get_text(" ", strip=True)
            heading = a.find(["h1", "h2", "h3", "h4"])
            title = (heading.get_text(" ", strip=True) if heading else raw_text)
            m = _MONTH_RE.search(raw_text) or _MONTH_RE.search(title)
            if not m:
                # 日期常放在卡片容器里而不是 <a> 文本里，往上找一层再试
                card = a.find_parent(["li", "div", "article", "section"])
                if card is not None:
                    m = _MONTH_RE.search(card.get_text(" ", strip=True)[:400])
            pub_dt, pub_str = None, ""
            if m:
                from ..normalize import parse_date_flexible
                pub_dt = parse_date_flexible(m.group(1))
                pub_str = fmt_date(pub_dt)
                title = _MONTH_RE.sub("", title).strip()
            # 卡片里常带栏目名（Announcements/Policy/Product），标题若过短则用卡片文本兜底
            if len(title) < 8:
                title = _MONTH_RE.sub("", raw_text).strip()
            if not title:
                continue
            title = strip_title_prefix(title) or title
            seen.add(href)
            out["entries_seen"] += 1
            if pub_dt and pub_dt < start_dt:
                continue
            layer, tags = classify(f"{title} {raw_text}", title)
            rec = {
                "title": title[:300],
                "url": href,
                "summary": strip_html(raw_text, limit=2000),
                "jurisdiction": "GLOBAL",
                "jurisdiction_name": "全球（企业信号）",
                "level": "企业",
                "issuing_body": company,
                "policy_type": "企业博客",
                "language": conf.get("language") or "en",
                "published_date": pub_str,
                "source_name": name,
                "collection_method": "company_html",
                "source_type": "corporate_blog",
                "signal_layer": layer,
                "company": company,
                "topic_tags": tags,
                "extra": {"blog": conf.get("list_url", "")},
            }
            if signal_store.insert(rec, keywords_cfg) == "new":
                out["new"] += 1
        return out
    except Exception as e:  # noqa: BLE001
        out["status"] = "failed"
        out["detail"] = f"{type(e).__name__}: {e}"
        return out


# ---------------- 全文抓取 ----------------
MIN_ARTICLE_CHARS = 400  # 低于这个长度视为"选择器没抓到正文"，触发降级重抽


def _longest_container(soup) -> str:
    """整页里挑文本量最大的容器（导航/推荐区用字数过滤不掉，靠启发式兜底）。"""
    best, best_len = "", 0
    for cand in soup.find_all(["article", "main", "div", "section"]):
        n = len(cand.get_text(" ", strip=True))
        if n > best_len:
            best, best_len = cand, n
    return best.get_text("\n", strip=True) if best_len else ""


def extract_article(html: str, selector: str = "article") -> str:
    """从文章页抽正文：优先站点选择器；选择器抓到的东西太短就降级重抽。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()
    text = ""
    node = soup.select_one(selector) if selector else None
    if node is not None:
        text = node.get_text("\n", strip=True)
    # 选择器缺失或抓到的正文太短（常见于 /index/ 汇总页、登录墙），换"最长容器"
    if len(text) < MIN_ARTICLE_CHARS:
        alt = _longest_container(soup)
        if len(alt) > len(text):
            text = alt
    if not text:
        text = soup.get_text("\n", strip=True)
    return clean_article_text(text)


# 页面噪声：按钮/占位/订阅提示，整行丢弃
_BOILERPLATE_RE = re.compile(
    r"^(?:Loading…?|Loading\.\.\.|Share|Share this(?: article)?|Copy link|Subscribe|"
    r"Sign up|Sign in|Back to top|Read more|Learn more)$", re.I)
# "推荐阅读"区块：两种排法都要认
#  A) 一行：栏目名 + 日期（"Product Oct 7, 2026 ..."）
#  B) 三行：栏目名 / 日期 / 标题（OpenAI 文章页底部就是这种）
_CAT = (r"Company|Product|Research|Publication|Global Affairs|API|Startup|Announcements|"
        r"Policy|Safety|News|Customers|Events|Community|Engineering")
_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_RELATED_LINE_RE = re.compile(
    rf"^(?:{_CAT})\s+{_MON}[a-z]*\.?\s+\d{{1,2}},\s+\d{{4}}\b.*$", re.I)
_CAT_LINE_RE = re.compile(rf"^(?:{_CAT})$", re.I)
_DATE_LINE_RE = re.compile(rf"^{_MON}[a-z]*\.?\s+\d{{1,2}},\s+\d{{4}}$", re.I)
# 标题被排版成逐字母散开（"C h a t G P T"），还原成连续字符
_SPACED_RE = re.compile(r"\b(?:[A-Za-z0-9] ){3,}[A-Za-z0-9]\b")


def _related_block_start(lines: list) -> int:
    """返回"推荐阅读"区块的起始行号，找不到返回 -1。"""
    hits = []
    for i, ln in enumerate(lines):
        if _RELATED_LINE_RE.match(ln):
            hits.append(i)
        elif (_CAT_LINE_RE.match(ln) and i + 1 < len(lines)
              and _DATE_LINE_RE.match(lines[i + 1])):
            hits.append(i)
    if not hits:
        return -1
    first = hits[0]
    # 正文前 40% 出现的不算（可能是句首恰好是 Product/Research）
    return first if first > max(8, len(lines) * 2 // 5) and len(hits) >= 2 else -1


def clean_article_text(text: str) -> str:
    """去掉正文里的排版噪声：空白/按钮词/推荐阅读块/散开的字母标题。

    纯函数、幂等（重复清洗不会继续删内容），便于对历史数据回溯清洗。
    """
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines()]
    # 1) 丢弃空行与按钮/占位词
    lines = [ln for ln in lines if ln and not _BOILERPLATE_RE.match(ln)]
    # 2) 截掉文末"推荐阅读"块
    start = _related_block_start(lines)
    if start > 0:
        lines = lines[:start]
    # 3) 散开的字母标题还原
    lines = [_SPACED_RE.sub(lambda m: m.group(0).replace(" ", ""), ln) for ln in lines]
    # 4) 连续重复行去重
    out, prev = [], None
    for ln in lines:
        if ln != prev:
            out.append(ln)
        prev = ln
    return "\n".join(out)


def _fulltext_one(rec: dict, selector_map: dict, conf_map: dict = None) -> tuple:
    guid, url, company = rec["guid"], rec["url"], rec.get("company", "")
    conf = dict((conf_map or {}).get(company) or {})
    conf.setdefault("needs_proxy", False)
    if not url:
        return guid, "", "failed:无链接"
    try:
        resp = _get(url, conf, timeout=40)
        text = extract_article(resp.text, selector_map.get(company, "article"))
        if len(text) < 200:
            hint = "403 反爬" if "Enable JavaScript" in text[:200] else "疑似反爬或空页"
            return guid, "", f"failed:正文过短({hint})"
        return guid, text, "ok"
    except requests.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", "")
        return guid, "", f"failed:HTTP{code}" if code else "failed:HTTPError"
    except Exception as e:  # noqa: BLE001
        return guid, "", f"failed:{type(e).__name__}"


def run_fulltext(limit: int = 5000, workers: int = 4) -> dict:
    cfg = load_config()
    blogs = cfg.get("company_blogs") or []
    selector_map = {b.get("company") or b["name"]: b.get("article_selector", "article") for b in blogs}
    conf_map = {b.get("company") or b["name"]: b for b in blogs}
    targets = signal_store.missing_fulltext(limit=limit)
    total = len(targets)
    print(f"📄 信号层待抓全文 {total} 条（workers={workers}）")
    if total == 0:
        return {"done": 0, "failed": 0}
    done = failed = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_fulltext_one, rec, selector_map, conf_map)
                       for _, rec in targets.iterrows()]
            for i, fut in enumerate(as_completed(futures), 1):
                guid, text, status = fut.result()
                signal_store.set_fulltext(guid, text, status)
                if status == "ok":
                    done += 1
                else:
                    failed += 1
                if i % 50 == 0:
                    print(f"  进度 {i}/{total}（成功 {done}，失败 {failed}）")
                if i % 100 == 0:
                    signal_store.save()
    finally:
        signal_store.save()
    print(f"📄 信号层全文结束：成功 {done}，失败 {failed}（失败的下轮自动重试）")
    return {"done": done, "failed": failed}


# ---------------- 主入口 ----------------
def reclean_stored() -> dict:
    """对已入库全文做一次离线清洗（不联网），用于修正历史抓取里的排版噪声。"""
    df = signal_store.load()
    if df.empty:
        print("📄 信号层为空，无需清洗")
        return {"scanned": 0, "changed": 0}
    changed = scanned = 0
    for _, rec in df.iterrows():
        old_title = rec.get("title") or ""
        new_title = strip_title_prefix(old_title) or old_title
        if new_title != old_title:
            signal_store.set_title(rec["guid"], new_title)
            changed += 1
        # 规则调整后回溯重算分层/标签（用标题 + 摘要，与在线采集口径一致）
        layer, tags = classify(f"{new_title} {rec.get('summary') or ''}", new_title)
        if layer != (rec.get("signal_layer") or "") or tags != (rec.get("topic_tags") or ""):
            signal_store.set_classification(rec["guid"], layer, tags)
            changed += 1
        old = rec.get("full_text") or ""
        if not old:
            continue
        scanned += 1
        new = clean_article_text(old)
        if new != old:
            signal_store.set_fulltext(rec["guid"], new, rec.get("fulltext_status") or "ok")
            changed += 1
    signal_store.save()
    print(f"🧽 离线清洗：扫描 {scanned} 条，改动 {changed} 条")
    return {"scanned": scanned, "changed": changed}


def run(only: str = "", collect: bool = True, fulltext: bool = False,
        limit: int = 5000, workers: int = 4, clean: bool = False) -> dict:
    cfg = load_config()
    start_dt = cfg["settings"]["start_date_dt"]
    kw = cfg["_keywords"]
    blogs = [b for b in (cfg.get("company_blogs") or []) if b.get("enabled", True)]
    if only:
        blogs = [b for b in blogs if only.lower() in (b.get("name", "") + b.get("company", "")).lower()]
    results = []
    if collect:
        print(f"🚀 企业信号采集启动（窗口起点 {cfg['settings']['start_date']}，启用 {len(blogs)} 家）")
        for conf in blogs:
            mode = conf.get("mode", "rss")
            r = _collect_rss(conf, start_dt, kw) if mode == "rss" else _collect_html(conf, start_dt, kw)
            mark = "✅" if r["status"] == "ok" else "❌"
            extra = f" +{r['new']} 新增" + (f"，AI 过滤 {r['skipped']} 条" if r.get("skipped") else "")
            print(f"  {mark} {r['source']}: 看到 {r['entries_seen']} 条{extra} {r['detail']}")
            signal_store.record_health(r["source"], conf.get("feed_url") or conf.get("list_url", ""),
                                       r["status"], r["detail"], r["entries_seen"], r["new"])
            results.append(r)
        signal_store.save()
    ft = {"done": 0, "failed": 0}
    cleaned = {"scanned": 0, "changed": 0}
    if clean:
        cleaned = reclean_stored()
    elif fulltext:
        ft = run_fulltext(limit=limit, workers=workers)
    st = signal_store.stats()
    print(f"📊 信号层现状：{st['total']} 条｜全文 ok {st['fulltext_ok']}｜按公司 {st['by_company']}")
    return {"results": results, "ok": sum(1 for r in results if r["status"] == "ok"),
            "failed": sum(1 for r in results if r["status"] != "ok"),
            "new": sum(r["new"] for r in results), "fulltext": ft, "cleaned": cleaned, "stats": st}
