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
    scope = "仅需代理的源" if args.proxy_only else "全部源"
    print(f"🚀 RSS 增量采集启动（{scope}，窗口起点 {cfg['settings']['start_date']}）")
    summary = rss_source.run(check_only=False, only_proxy=args.proxy_only)
    _finish("update", summary)
    print(f"✅ 增量完成：源 {summary['ok']} 成功 / {summary['failed']} 失败，新增 {summary['new']} 条")
    return 0


def cmd_backfill(args) -> int:
    from .sources import govcn, federal_register, oecd, govuk, oecd_overview
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
    if target in ("govuk", "all"):
        print("— 英国 gov.uk 政策检索 —")
        r = govuk.run()
        ok += r["ok"]; failed += r["failed"]; new += r["new"]
    if target in ("oecd-overview", "all"):
        print("— OECD 概述正文回填（补 SPA 无正文的记录）—")
        r = oecd_overview.run()
        ok += r["ok"]; failed += r["failed"]; new += r.get("updated", 0)
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
    exporter.run(since=args.since, xlsx=args.xlsx, browse=not args.no_browse)
    if args.signals:
        exporter.run_signals(xlsx=args.xlsx)
    return 0


def cmd_blog(args) -> int:
    from .sources import company_blog
    r = company_blog.run(only=args.only, collect=not args.clean, fulltext=args.fulltext,
                         limit=args.limit, workers=args.workers, clean=args.clean)
    store.log_run("blog", r["ok"], r["failed"], r["new"],
                  f"全文成功 {r['fulltext']['done']} / 失败 {r['fulltext']['failed']}")
    store.save()
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

    p = sub.add_parser("update", help="RSS 增量采集")
    p.add_argument("--proxy-only", action="store_true",
                   help="只采集 needs_proxy 的源（配合 settings.proxy，专门为需代理的站点开代理）")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("backfill", help="历史回填（govcn / federal / oecd / govuk / oecd-overview / all）")
    p.add_argument("target",
                   choices=["govcn", "federal", "oecd", "govuk", "oecd-overview", "all"],
                   default="all", nargs="?")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("fulltext", help="抓取政策全文写回主表")
    p.add_argument("--limit", type=int, default=10000)
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(func=cmd_fulltext)

    p = sub.add_parser("export", help="导出主表 CSV 快照")
    p.add_argument("--since", default="", help="起始日期 YYYY-MM-DD，默认读配置")
    p.add_argument("--xlsx", action="store_true", help="同时导出 Excel 浏览视图（概览+分国家+全文截断预览）")
    p.add_argument("--no-browse", action="store_true", help="不导出浏览版 CSV（默认导出，Excel 可直接打开）")
    p.add_argument("--signals", action="store_true", help="同时导出信号层（六大巨头博客）视图")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("blog", help="采集六大科技巨头官方博客（信号层，独立表）")
    p.add_argument("--fulltext", action="store_true", help="采集后立即补抓全文（推荐：全文才是可用记录）")
    p.add_argument("--only", default="", help="只跑某一家，如 --only OpenAI")
    p.add_argument("--limit", type=int, default=5000, help="本轮最多补抓多少条全文")
    p.add_argument("--workers", type=int, default=4, help="全文抓取并发数")
    p.add_argument("--clean", action="store_true",
                   help="只对已入库全文做离线清洗（去导航/推荐块/散开字母），不联网")
    p.set_defaults(func=cmd_blog)

    sub.add_parser("report", help="生成采集报告").set_defaults(func=cmd_report)
    sub.add_parser("check", help="源连通性检查").set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
