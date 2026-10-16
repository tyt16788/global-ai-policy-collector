# -*- coding: utf-8 -*-
"""加载 config.yaml，提供路径与配置的统一入口。"""

import os
from datetime import datetime
from pathlib import Path

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
    "db_path": "data/policy.sqlite",
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


def get_proxy() -> str:
    """返回代理地址：config 优先，其次系统环境变量，都没有则空串。"""
    cfg = load_config()
    return cfg["settings"].get("proxy") or os.environ.get("HTTPS_PROXY", "") or os.environ.get("https_proxy", "")
