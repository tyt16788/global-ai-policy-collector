# -*- coding: utf-8 -*-
"""带重试 / 随机 UA / 代理 / 礼貌延时的统一 HTTP 抓取器。"""

import random
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import load_config

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def get_session() -> requests.Session:
    """创建带重试与代理的 Session（继承自 2.py 的做法）。"""
    cfg = load_config()
    s = cfg["settings"]
    session = requests.Session()
    retry = Retry(
        total=s["retry"],
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    proxy = s.get("proxy") or ""
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def polite_sleep():
    lo, hi = load_config()["settings"]["politeness_delay"]
    time.sleep(random.uniform(float(lo), float(hi)))


def http_get(url: str, session: Optional[requests.Session] = None,
             referer: Optional[str] = None, timeout: Optional[int] = None,
             headers: Optional[dict] = None) -> requests.Response:
    """GET 请求：随机 UA + 可选 Referer，非 2xx 抛异常。"""
    own_session = session is None
    session = session or get_session()
    cfg = load_config()
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/rss+xml, application/xml;q=0.9, application/json;q=0.9, */*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    try:
        resp = session.get(url, headers=h, timeout=timeout or cfg["settings"]["timeout"])
        resp.raise_for_status()
        return resp
    finally:
        if own_session:
            session.close()
