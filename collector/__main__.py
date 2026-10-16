# -*- coding: utf-8 -*-
"""CLI 入口。

用法（在项目根目录 rss_spider/ 下执行）：
  python -m collector update                 # RSS 增量采集
  python -m collector backfill all           # 回填：govcn + federal + oecd
  python -m collector backfill govcn         # 只回填 gov.cn 政策文件库
  python -m collector fulltext               # 抓取缺失全文（失败自动重试）
  python -m collector fulltext --limit 100   # 只抓 100 条
  python -m collector export                 # 导出主表 CSV 快照
  python -m collector export --xlsx          # 同时导出 Excel 浏览视图（全文截断）
  python -m collector report                 # 生成采集报告
  python -m collector check                  # 只测源连通性，不入库

数据存储：data/policy_master.csv 单一主表（含全文列），无 SQL。
"""

import argparse
import sys

from . import store, __version__
from .config import load_config


def _finish(command: str, summary: dict):
    parts = []
    for r in summary.get("results", []):
        # rss_source 返回 dict，其余源返回已格式化的字符串
        parts.append(r if isinstance(r, str)
                     else f"{r.get('source', '?')}:{r.get('status', '?')}(+{r.get('new', 0)})")
    store.log_run(command, summary.get("ok", 0), summary.get("failed", 0),
                  summary.get("new", 0), "; ".join(parts)[:500])
    store.save()


def cmd_update(args) -> int:
    from .sources import rss_source
    cfg = load_config()
    print(f"🚀 RSS 增量采集启动（窗口起点 {cfg['settings']['start_date']}）")
    summary = rss_source.run(check_only=False)
    _finish("update", summary)
    print(f"✅ 增量完成：源 {summary['ok']} 成功 / {summary['failed']} 失败，新增 {summary['new']} 条")
    return 0


def cmd_backfill(args) -> int:
    from .sources import govcn, federal_register, oecd
    target = args.target
    print(f"🚀 历史回填启动：{target}")
    ok = failed = new = 0
    if target in ("govcn", "all"):
        print("— gov.cn 国务院政策文件库 —")
        r = govcn.run()
        ok += r["ok"]; failed += r["failed"]; new += r["new"]
    if target in ("federal", "all"):
        print("— 美国 Federal Register —")
        r = federal_register.run()
        ok += r["ok"]; failed += r["failed"]; new += r["new"]
    if target in ("oecd", "all"):
        print("— OECD.AI 政策观察站 —")
        r = oecd.run()
        ok += r["ok"]; failed += r["failed"]; new += r["new"]
    _finish(f"backfill {target}", {"ok": ok, "failed": failed, "new": new})
    print(f"✅ 回填完成：{ok} 成功 / {failed} 失败，新增 {new} 条")
    return 0


def cmd_fulltext(args) -> int:
    from . import fulltext
    r = fulltext.run(limit=args.limit, workers=args.workers)
    _finish("fulltext", {"ok": r["done"], "failed": r["failed"], "new": 0})
    return 0


def cmd_export(args) -> int:
    from . import exporter
    exporter.run(since=args.since, xlsx=args.xlsx)
    return 0


def cmd_report(args) -> int:
    from . import report
    report.run()
    return 0


def cmd_check(args) -> int:
    from .sources import rss_source
    print("🔍 源连通性检查（不入库）")
    summary = rss_source.run(check_only=True)
    print(f"✅ 检查完成：{summary['ok']} 可用 / {summary['failed']} 失败")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="collector", description="全球 AI 政策采集系统 v5（CSV 存储）")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("update", help="RSS 增量采集").set_defaults(func=cmd_update)

    p = sub.add_parser("backfill", help="历史回填（govcn / federal / oecd / all）")
    p.add_argument("target", choices=["govcn", "federal", "oecd", "all"], default="all", nargs="?")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("fulltext", help="抓取政策全文写回主表")
    p.add_argument("--limit", type=int, default=10000)
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(func=cmd_fulltext)

    p = sub.add_parser("export", help="导出主表 CSV 快照")
    p.add_argument("--since", default="", help="起始日期 YYYY-MM-DD，默认读配置")
    p.add_argument("--xlsx", action="store_true", help="同时导出 Excel 浏览视图")
    p.set_defaults(func=cmd_export)

    sub.add_parser("report", help="生成采集报告").set_defaults(func=cmd_report)
    sub.add_parser("check", help="源连通性检查").set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
