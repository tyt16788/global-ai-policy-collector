# -*- coding: utf-8 -*-
"""全文抓取：为每条政策抓原文，写回主表 full_text 列。

获取策略（优先官方接口，其次爬页面）：
- federal_register_api：extra_json.raw_text_url 官方纯文本接口（剥掉 HTML 壳）
- govcn_api：爬 gov.cn 政策页，取 #UCAP-CONTENT 正文容器
- 其余 RSS/接口来源：爬链接页，启发式抽取正文

速率控制：同域名最多 2 并发、最小间隔 0.8 秒；fulltext_status != ok 的记录每轮自动重试。
注意：主表含全文列，请勿用 Excel 打开 policy_master.csv 后直接保存。
"""

import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .fetcher import get_session
from .normalize import strip_html
from . import store

# 每域名：最小请求间隔（秒）与最大并发
DOMAIN_MIN_INTERVAL = 0.8
DOMAIN_MAX_CONCURRENT = 2

_domain_locks: dict = defaultdict(threading.Lock)
# TODO(并发): _domain_sems 目前**没有任何地方 acquire**，同域名实际并发上限由
# FETCH_WORKERS 决定（8），而不是这里的 2。要真正限并发需把请求包进
# `with _domain_sems[domain]:`，但那会明显拉长单轮耗时，改前先确认 workflow 的
# 120 分钟预算够用。先保留声明，避免误读成「已经限流了」。
_domain_sems: dict = defaultdict(lambda: threading.Semaphore(DOMAIN_MAX_CONCURRENT))
_last_req: dict = defaultdict(float)


def _rate_limit(domain: str):
    """同域名下：串行化并保证最小请求间隔（并发上限见上面的 TODO）。"""
    with _domain_locks[domain]:
        wait = DOMAIN_MIN_INTERVAL - (time.monotonic() - _last_req[domain])
        if wait > 0:
            time.sleep(wait)
        _last_req[domain] = time.monotonic()


def _domain_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def extract_main_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    node = soup.find(id="UCAP-CONTENT") or soup.find(id="content")
    if node:
        text = node.get_text("\n", strip=True)
        if len(text) > 200:
            return text
    best, best_len = None, 0
    for cand in soup.find_all(["div", "article", "main", "section"]):
        n = len(cand.get_text(strip=True))
        if n > best_len:
            best, best_len = cand, n
    if best:
        return best.get_text("\n", strip=True)
    return soup.get_text("\n", strip=True)


def extract_federal_raw(html: str) -> str:
    """FR raw_text 接口返回 <html><head>...</head><body><pre>正文</pre></body></html>，取 <pre> 内层。"""
    m = re.search(r"<pre[^>]*>(.*)</pre>", html, re.S | re.I)
    if m:
        return m.group(1).strip()
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text("\n", strip=True)


# 这些 URL 是 SPA 仪表盘，服务端不返回正文，抓了也只有壳
_DASHBOARD_PREFIXES = (
    "https://oecd.ai/en/dashboards/policy-initiatives",
    "https://oecd.ai/en/dashboards",
)


# requests 遇到不带 charset 的 `Content-Type: text/html` 时会按 RFC 2616 把
# encoding 填成 ISO-8859-1（不是 None），所以「没写 charset」和「真的是 latin-1」
# 在 requests 眼里长得一模一样。这几种值都视为「未声明编码」，交给嗅探重判。
_NO_CHARSET_MARKERS = ("", "iso-8859-1", "latin-1", "ascii")


def _decode(resp) -> str:
    """按真实编码取文本，替代直接用 `resp.text`。

    Bug 现场（2026-11 体检）：gov.cn / stats.gov.cn 的响应头只有 `text/html`，
    requests 会把 `resp.encoding` 设成 ISO-8859-1，于是 `resp.text` 整篇乱码 ——
    「国家知识产权局关于加强…」被写成「å½å®¶ç¥è¯äº§æå±å±å³äº…」，
    而 `_crawl_one` 只要长度 >100 就判定 ok，乱码照样入库，主表 1,949 条正文报废。

    旧代码 `resp.encoding = resp.encoding or "utf-8"` 之所以完全无效，
    正是因为 ISO-8859-1 是**真值**，`or` 永远走不到兜底分支。

    修法：先用 chardet（`apparent_encoding`）嗅探真实编码；嗅探失败再退 utf-8。
    只在「服务端未声明编码」时才动手，已明确声明 utf-8/gbk 的响应保持原判，
    避免把本来就正确的页面嗅探坏。
    """
    if (resp.encoding or "").lower() in _NO_CHARSET_MARKERS:
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _crawl_one(rec: dict, session) -> tuple:
    """返回 (guid, full_text, status)。status: ok / failed:<原因>"""
    guid, url = rec["guid"], rec["url"]
    if url.startswith(_DASHBOARD_PREFIXES):
        return guid, "", "failed:仪表盘页面无静态正文"
    try:
        if rec["collection_method"] == "federal_register_api":
            try:
                raw_url = json.loads(rec.get("extra_json") or "{}").get("raw_text_url", "")
            except json.JSONDecodeError:
                raw_url = ""
            target = raw_url or url
            _rate_limit(_domain_of(target))
            resp = session.get(target, timeout=15)
            resp.raise_for_status()
            # 一律走 _decode()：直接用 resp.text 会把不带 charset 的 GBK 页面读成乱码
            body = _decode(resp)
            text = extract_federal_raw(body) if raw_url else extract_main_text(body)
        else:
            _rate_limit(_domain_of(url))
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            text = extract_main_text(_decode(resp))

        text = strip_html(text).strip() if "<" in text[:200] else text.strip()
        if len(text) < 100:
            return guid, "", "failed:正文过短(疑似反爬或空页)"
        return guid, text, "ok"
    except Exception as e:  # noqa: BLE001 — 单条失败不中断整体
        return guid, "", f"failed:{type(e).__name__}"


def _light_session():
    """全文抓取专用 session：只重试 1 次，避免死链把整批拖到小时级。"""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = get_session()
    retry = Retry(total=1, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["HEAD", "GET", "OPTIONS"])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def run(limit: int = 10_000, workers: int = 8, save_every: int = 200) -> dict:
    targets = store.records_missing_fulltext(limit=limit)
    total = len(targets)
    print(f"📄 待抓全文 {total} 条（workers={workers}）")
    if total == 0:
        return {"done": 0, "failed": 0}

    session = _light_session()
    done = failed = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_crawl_one, rec, session) for _, rec in targets.iterrows()]
            for i, fut in enumerate(as_completed(futures), 1):
                guid, text, status = fut.result()
                store.set_fulltext(guid, text, status)
                if status == "ok":
                    done += 1
                else:
                    failed += 1
                if i % 50 == 0:
                    print(f"  进度 {i}/{total}（成功 {done}，失败 {failed}）")
                if i % save_every == 0:
                    store.save()
    finally:
        session.close()
        store.save()

    print(f"📄 全文抓取结束：成功 {done}，失败 {failed}（失败的下一轮会自动重试）")
    return {"done": done, "failed": failed}
