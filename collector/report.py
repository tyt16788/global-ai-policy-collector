# -*- coding: utf-8 -*-
"""采集报告：库统计 + 全文进度 + 源健康状况 + 疑似重复，输出 Markdown 到 exports/。"""

from datetime import datetime

from .config import load_config, resolve_path
from . import store


def run() -> dict:
    s = store.stats()
    health = store.latest_health()
    dups = store.suspected_duplicates()
    export_dir = resolve_path(load_config()["settings"]["export_dir"])
    out = export_dir / f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"

    lines = [
        f"# AI 政策采集报告 — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"**库内总条数：{s['total']}**（日期待定 {s['no_date']} 条）",
        "",
        f"**全文进度：已抓 {s['fulltext_ok']} / 待抓与待重试 {s['total'] - s['fulltext_ok']}"
        f"（其中失败待重试 {s['fulltext_failed']}）**",
        "",
        "## 按国家/地区", "", "| 国家代码 | 条数 |", "|---|---|",
    ]
    for k, v in s["by_jurisdiction"].items():
        lines.append(f"| {k} | {v} |")

    lines += ["", "## 按年份（按发布日期）", "", "| 年份 | 条数 |", "|---|---|"]
    for k, v in s["by_year"].items():
        lines.append(f"| {k or '未知'} | {v} |")

    lines += ["", "## 按采集方式", "", "| 方式 | 条数 |", "|---|---|"]
    for k, v in s["by_method"].items():
        lines.append(f"| {k or '未知'} | {v} |")

    lines += ["", "## 源健康状况（每源最近一次）", "",
              "| 源 | 状态 | 原始条数 | 新增 | 详情 |", "|---|---|---|---|---|"]
    for h in health:
        mark = "✅" if h["status"] == "ok" else "❌"
        detail = (h["detail"] or "").replace("|", "\\|")[:120]
        lines.append(f"| {h['source_name']} | {mark} | {h['entries_seen']} | {h['entries_new']} | {detail} |")

    lines += ["", f"## 疑似重复（同国家+同标题+同日期，不同网址）：{len(dups)} 组", ""]
    for d in dups[:50]:
        lines.append(f"- [{d['jurisdiction']}] {d['title'][:60]}（{d['published_date']}，{d['n']} 条）")

    lines += [
        "",
        "## 失败源处理建议",
        "",
        "- 本地跑 OECD/英国源需代理：`config.yaml` 的 `settings.proxy` 填代理地址后重跑；GitHub Actions 云端无需代理。",
        "- OECD 端点失效时：浏览器打开 oecd.ai 政策页 → F12 Network 找 JSON 请求 → 更新 `config.yaml` 的 `oecd.endpoint`。",
        "- 全文失败的记录每轮 `fulltext` 命令自动重试；持续失败的源（如反爬站点）在全文状态列可见原因。",
    ]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"📊 报告已生成: {out.name}（源 {len(health)} 个，疑似重复 {len(dups)} 组，全文 {s['fulltext_ok']}/{s['total']}）")
    return {"path": str(out), "total": s["total"]}
