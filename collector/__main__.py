# -*- coding: utf-8 -*-
"""CLI 入口。

用法（在项目根目录 rss_spider/ 下执行）：
  python -m collector update                 # RSS 增量采集
  python -m collector backfill all           # 回填：govcn + federal + oecd
  python -m collector backfill govcn         # 只回填 gov.cn 政策文件库
  python -m collector fulltext --limit 200   # 补抓政策正文存 TXT
  python -m collector export                 # 导出 CSV + Excel
  python -m collector report                 # 生成采集报告
  python -m collector check                  # 只测源连通性，不入库
"""

import argparse
import sys

from . import db, __version__
from .config import load_config


def cmd_update(args) -> int:
    from .sources import rss_source
    cfg = load_config()
    db.init_db()
    run_id = db.start_run("update")
    print(f"🚀 RSS 增量采集启动（窗口起点 {cfg['settings']['start_date']}）")
    summary = rss_source.run(check_only=False)
    db.finish_run(run_id, summary["ok"], summary["failed"], summary["new"], summary)
    print(f"✅ 增量完成：源 {summary['ok']} 成功 / {summary['failed']} 失败，新增 {summary['new']} 条")
    return 0


def cmd_backfill(args) -> int:
    from .sources import govcn, federal_register, oecd
    db.init_db()
    target = args.target
    run_id = db.start_run(f"backfill {target}")
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
    db.finish_run(run_id, ok, failed, new, {"target": target})
    print(f"✅ 回填完成：{ok} 成功 / {failed} 失败，新增 {new} 条")
    return 0


def cmd_fulltext(args) -> int:
    from . import fulltext
    db.init_db()
    fulltext.run(limit=args.limit)
    return 0


def cmd_export(args) -> int:
    db.init_db()
    from . import exporter
    exporter.run(since=args.since)
    return 0


def cmd_report(args) -> int:
    db.init_db()
    from . import report
    report.run()
    return 0


def cmd_check(args) -> int:
    from .sources import rss_source
    db.init_db()
    print("🔍 源连通性检查（不入库）")
    summary = rss_source.run(check_only=True)
    print(f"✅ 检查完成：{summary['ok']} 可用 / {summary['failed']} 失败")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="collector", description="全球 AI 政策采集系统 v5")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("update", help="RSS 增量采集").set_defaults(func=cmd_update)

    p = sub.add_parser("backfill", help="历史回填（govcn / federal / oecd / all）")
    p.add_argument("target", choices=["govcn", "federal", "oecd", "all"], default="all", nargs="?")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("fulltext", help="补抓政策正文")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_fulltext)

    p = sub.add_parser("export", help="导出 CSV/Excel")
    p.add_argument("--since", default="", help="起始日期 YYYY-MM-DD，默认读配置")
    p.set_defaults(func=cmd_export)

    sub.add_parser("report", help="生成采集报告").set_defaults(func=cmd_report)
    sub.add_parser("check", help="源连通性检查").set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
