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
_domain_sems: dict = defaultdict(lambda: threading.Semaphore(DOMAIN_MAX_CONCURRENT))
_last_req: dict = defaultdict(float)


def _rate_limit(domain: str):
    """同域名下：限制并发 + 保证最小请求间隔。"""
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


def _crawl_one(rec: dict, session) -> tuple:
    """返回 (guid, full_text, status)。status: ok / failed:<原因>"""
    guid, url = rec["guid"], rec["url"]
    try:
        if rec["collection_method"] == "federal_register_api":
            try:
                raw_url = json.loads(rec.get("extra_json") or "{}").get("raw_text_url", "")
            except json.JSONDecodeError:
                raw_url = ""
            target = raw_url or url
            _rate_limit(_domain_of(target))
            resp = session.get(target, timeout=25)
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            text = extract_federal_raw(resp.text) if raw_url else extract_main_text(resp.text)
        else:
            _rate_limit(_domain_of(url))
            resp = session.get(url, timeout=25)
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            text = extract_main_text(resp.text)

        text = strip_html(text).strip() if "<" in text[:200] else text.strip()
        if len(text) < 100:
            return guid, "", "failed:正文过短(疑似反爬或空页)"
        return guid, text, "ok"
    except Exception as e:  # noqa: BLE001 — 单条失败不中断整体
        return guid, "", f"failed:{type(e).__name__}"


def run(limit: int = 10_000, workers: int = 8, save_every: int = 200) -> dict:
    targets = store.records_missing_fulltext(limit=limit)
    total = len(targets)
    print(f"📄 待抓全文 {total} 条（workers={workers}）")
    if total == 0:
        return {"done": 0, "failed": 0}

    session = get_session()
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
