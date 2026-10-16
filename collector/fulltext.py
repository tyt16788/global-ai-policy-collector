# -*- coding: utf-8 -*-
"""可选全文抓取：把政策详情页正文存为 TXT，登记到主库 full_text_path 字段。

gov.cn 政策库正文在 <div id="UCAP-CONTENT">；其他站点退化为主文本块启发式抽取。
用法：python -m collector fulltext [--limit N]
"""

from pathlib import Path

from bs4 import BeautifulSoup

from .config import load_config, resolve_path
from .fetcher import http_get
from . import db


def extract_main_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    # gov.cn 政策库专用容器
    node = soup.find(id="UCAP-CONTENT") or soup.find(id="content")
    if node:
        text = node.get_text("\n", strip=True)
        if len(text) > 200:
            return text

    # 启发式：取文本量最大的 div/article
    best, best_len = None, 0
    for cand in soup.find_all(["div", "article", "main", "section"]):
        n = len(cand.get_text(strip=True))
        # 剔除嵌套被重复计数的浅层节点：只看无子容器的"叶子块"权重不高，直接取最大即可
        if n > best_len:
            best, best_len = cand, n
    if best:
        return best.get_text("\n", strip=True)
    return soup.get_text("\n", strip=True)


def run(limit: int = 200) -> dict:
    cfg = load_config()
    out_dir = resolve_path(cfg["settings"]["fulltext_dir"])
    targets = db.policies_missing_fulltext(limit=limit)
    print(f"📄 待抓全文 {len(targets)} 条（limit={limit}）")

    done = failed = 0
    for i, t in enumerate(targets, 1):
        guid, url = t["guid"], t["url"]
        try:
            resp = http_get(url, timeout=20)
            html = resp.text
            if html:
                text = extract_main_text(html)
                if len(text) >= 100:  # 太短视为反爬页/空页
                    out_file = out_dir / f"{guid[:16]}.txt"
                    out_file.write_text(text, encoding="utf-8")
                    db.set_fulltext_path(guid, str(out_file.relative_to(out_dir.parents[1])))
                    done += 1
                    if i % 20 == 0:
                        print(f"  已完成 {i}/{len(targets)}（成功 {done} 失败 {failed}）")
                    continue
        except Exception:  # noqa: BLE001 — 单条失败不中断
            pass
        failed += 1

    print(f"📄 全文抓取结束：成功 {done}，失败 {failed}")
    return {"done": done, "failed": failed}
