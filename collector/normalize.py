# -*- coding: utf-8 -*-
"""规范化工具：URL 归一化、去重键、日期解析、HTML 清洗、语言识别、AI 打标。"""

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# 需要从 URL 中剔除的跟踪参数
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "spm", "from", "isFromOtherWeb", "refer_flag",
}


def normalize_url(url: str) -> str:
    """URL 归一化：小写主机、去跟踪参数、排序查询参数、去 fragment、统一 https、去尾部斜杠。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()

    scheme = "https"
    netloc = parts.netloc.lower()
    # 去掉端口默认值
    if netloc.endswith(":80") or netloc.endswith(":443"):
        netloc = netloc.rsplit(":", 1)[0]

    path = parts.path or "/"
    # 保留多级路径，只去尾部斜杠（根路径除外）
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k not in _TRACKING_PARAMS]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def make_guid(url: str, fallback_title: str = "", fallback_date: str = "") -> str:
    """去重主键：优先规范化 URL 的哈希；无 URL 时退化为 标题+日期 哈希。"""
    norm = normalize_url(url)
    if norm:
        basis = "url:" + norm
    else:
        basis = "meta:" + fallback_title.strip().lower() + "|" + fallback_date
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def make_content_hash(jurisdiction: str, title: str, published_date: str) -> str:
    """内容指纹：同一文件出现在不同 URL（如政策库与部委官网）时用于疑似重复报告。"""
    t = re.sub(r"\s+", "", (title or "").lower())
    return hashlib.sha256(f"{jurisdiction}|{t}|{published_date}".encode("utf-8")).hexdigest()


# ---------------- 日期解析 ----------------
_DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y.%m.%d",
    "%Y年%m月%d日 %H:%M", "%Y年%m月%d日",
]


def parse_date_flexible(date_str: str) -> Optional[datetime]:
    """尽量把各种日期字符串解析成 datetime（统一转成 UTC 朴素时间），失败返回 None。"""
    if not date_str:
        return None
    s = str(date_str).strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # epoch 毫秒/秒
    m = re.fullmatch(r"1[0-9]{12}", s)
    if m:
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).replace(tzinfo=None)
    return None


def fmt_date(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


# ---------------- HTML / 文本清洗 ----------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(html: str, limit: int = 0) -> str:
    """去掉 HTML 标签与多余空白，可选截断。"""
    if not html:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()
    return text[:limit] if limit else text


# ---------------- 语言识别（简单启发式） ----------------
def guess_language(text: str, default: str = "") -> str:
    if not text:
        return default
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    kana = sum(1 for ch in text if "\u3040" <= ch <= "\u30ff")
    hangul = sum(1 for ch in text if "\uac00" <= ch <= "\ud7af")
    n = max(len(text), 1)
    if kana / n > 0.05:
        return "ja"
    if hangul / n > 0.05:
        return "ko"
    if cjk / n > 0.15:
        return "zh"
    return default or ("en" if re.search(r"[A-Za-z]", text) else "")


# ---------------- AI 关键词打标（只标注，不过滤） ----------------
def tag_ai(text: str, keywords_cfg: dict) -> str:
    """中文子串匹配；英文用 \\b 词边界匹配，避免 "said" 误命中 "ai"。"""
    if not text:
        return ""
    lowered = text.lower()
    tags = []
    for kw in (keywords_cfg.get("zh") or []):
        if kw in lowered and kw not in tags:
            tags.append(kw)
    for kw in (keywords_cfg.get("en") or []):
        core = kw.strip()
        if not core:
            continue
        try:
            if re.search(r"\b" + re.escape(core.lower()) + r"\b", lowered) and core not in tags:
                tags.append(core)
        except re.error:
            continue
    return ", ".join(tags)
