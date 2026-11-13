# -*- coding: utf-8 -*-
"""加载 config.yaml，提供路径与配置的统一入口。"""

import os
import socket
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml

# 项目根目录 = collector/ 的上一级
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

_DEFAULTS = {
    "start_date": "2023-01-01",
    "retry": 3,
    "timeout": 30,
    "politeness_delay": [1.0, 2.5],
    "max_workers": 4,
    "proxy": "",
    "master_csv": "data/policy_master.csv",
    "signal_csv": "data/signal_master.csv",
    "signal_health_csv": "data/signal_health.csv",
    "export_dir": "exports",
    "fulltext_dir": "data/fulltext",
}


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    settings = dict(_DEFAULTS)
    settings.update(cfg.get("settings") or {})
    settings["start_date_dt"] = datetime.strptime(settings["start_date"], "%Y-%m-%d")
    cfg["settings"] = settings
    cfg["_keywords"] = (cfg.get("keywords") or {})
    return cfg


def resolve_path(rel: str) -> Path:
    """把配置里的相对路径解析为项目根目录下的绝对路径，并确保父目录存在。"""
    p = Path(rel)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# 本机代理（Clash / V2Ray 之类）绑在 loopback 上，云端 CI 里根本不存在这个端口。
# 只要照着 config.yaml 用它，所有 needs_proxy 的源都会变成 ProxyError —— 2026-11-13
# 手动触发实测：全文抓取「成功 0 / 失败 200」，Federal Register / OECD / gov.uk 全灭。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}
_PROXY_PROBE_TIMEOUT = 0.35   # 秒；连本机端口，正常是毫秒级
_proxy_probe_cache: dict = {}
_warned_proxies: set = set()


def _proxy_reachable(proxy: str) -> bool:
    """探测代理端口是否在监听（按地址缓存，整轮只探一次）。

    只看 loopback 地址；远程代理（公司网关等）不做探活，避免误判把正常配置丢掉。
    """
    if proxy in _proxy_probe_cache:
        return _proxy_probe_cache[proxy]
    try:
        parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        host, port = "", 0
    if host not in _LOOPBACK_HOSTS:
        _proxy_probe_cache[proxy] = True
        return True
    try:
        with socket.create_connection((host.strip("[]"), port), timeout=_PROXY_PROBE_TIMEOUT):
            ok = True
    except OSError:
        ok = False
    _proxy_probe_cache[proxy] = ok
    return ok


def get_proxy() -> str:
    """返回**当前环境真的能用**的代理地址，没有则空串。

    优先级：COLLECTOR_PROXY 环境变量 → config.yaml 的 settings.proxy → HTTPS_PROXY。
    loopback 代理会先探活（结果按进程缓存），连不上就当没配置并提示一次：
    - 本地：Clash 没开时自动降级直连，不再满屏 ProxyError；
    - CI：127.0.0.1:7897 不存在 → 直接判空 → 各源走直连（云端本来就在墙外）。
    需要给 CI 配真代理时，设置 COLLECTOR_PROXY / HTTPS_PROXY 即可。
    """
    cfg = load_config()
    candidates = [
        os.environ.get("COLLECTOR_PROXY", ""),
        cfg["settings"].get("proxy") or "",
        os.environ.get("HTTPS_PROXY", "") or os.environ.get("https_proxy", ""),
    ]
    for proxy in candidates:
        proxy = (proxy or "").strip()
        if not proxy:
            continue
        if _proxy_reachable(proxy):
            return proxy
        if proxy not in _warned_proxies:
            _warned_proxies.add(proxy)
            print(f"⚠️ 代理 {proxy} 连不上（本机未启动代理，或当前环境无此端口），本次跳过它改走直连")
    return ""
