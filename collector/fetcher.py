# -*- coding: utf-8 -*-
"""带重试 / 随机 UA / 代理 / 礼貌延时的统一 HTTP 抓取器。"""

import os
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


def get_session(force_proxy: Optional[bool] = None) -> requests.Session:
    """创建带重试与代理的 Session（继承自 2.py 的做法）。

    force_proxy 控制代理策略，用于"只在采需代理的站时才开代理"：
    - None （默认）：沿用 settings.proxy / 环境变量，行为与旧版一致；
    - False：强制直连，连系统 HTTPS_PROXY 也不认（避免国内源被带出代理）；
    - True ：必须走代理，未配置代理直接报错（该源标记了 needs_proxy）。
    """
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
    if force_proxy is True and not proxy:
        proxy = os.environ.get("HTTPS_PROXY", "") or os.environ.get("https_proxy", "")
    if force_proxy is True and not proxy:
        session.close()
        raise RuntimeError("该源标记 needs_proxy，但 settings.proxy 未配置且无 HTTPS_PROXY 环境变量")

    if force_proxy is False:
        # 强制直连：忽略系统/环境变量里的代理
        session.trust_env = False
    else:
        proxy = proxy or ""
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
    return session


def polite_sleep():
    lo, hi = load_config()["settings"]["politeness_delay"]
    time.sleep(random.uniform(float(lo), float(hi)))


def http_get(url: str, session: Optional[requests.Session] = None,
             referer: Optional[str] = None, timeout: Optional[int] = None,
             headers: Optional[dict] = None,
             params: Optional[dict] = None) -> requests.Response:
    """GET 请求：随机 UA + 可选 Referer / 查询参数，非 2xx 抛异常。"""
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
        resp = session.get(url, headers=h, params=params,
                           timeout=timeout or cfg["settings"]["timeout"])
        resp.raise_for_status()
        return resp
    finally:
        if own_session:
            session.close()
