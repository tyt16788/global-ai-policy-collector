# -*- coding: utf-8 -*-
"""导出：主 CSV + 按国家分 sheet 的 Excel，含"日期待定"页。"""

from datetime import datetime

import pandas as pd

from .config import load_config, resolve_path
from . import db

COLS = ["title", "url", "summary", "published_date", "jurisdiction", "jurisdiction_name",
        "level", "issuing_body", "policy_type", "language", "ai_tags",
        "source_name", "collection_method", "full_text_path", "content_hash"]
COL_NAMES_ZH = ["标题", "网址", "摘要", "发布日期", "国家代码", "国家/地区", "层级",
                "发布机构", "政策类型", "语言", "AI标签", "来源", "采集方式",
                "全文路径", "内容指纹"]


def run(since: str = "") -> dict:
    cfg = load_config()
    since = since or cfg["settings"]["start_date"]
    export_dir = resolve_path(cfg["settings"]["export_dir"])
    dated, undated = db.fetch_all_for_export(since)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    master_csv = export_dir / f"policy_master_{stamp}.csv"
    xlsx_path = export_dir / f"政策库_{stamp}.xlsx"

    df_all = pd.DataFrame(dated + undated, columns=COLS)
    df_all.to_csv(master_csv, index=False, encoding="utf-8-sig")

    df_dated = pd.DataFrame(dated, columns=COLS).rename(columns=dict(zip(COLS, COL_NAMES_ZH)))
    df_undated = pd.DataFrame(undated, columns=COLS).rename(columns=dict(zip(COLS, COL_NAMES_ZH)))

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df_dated.to_excel(writer, sheet_name="全部", index=False)
        for juris, grp in df_dated.groupby("国家代码", sort=False):
            sheet = str(juris)[:31] or "未知"
            grp.to_excel(writer, sheet_name=sheet, index=False)
        if not df_undated.empty:
            df_undated.to_excel(writer, sheet_name="日期待定", index=False)

    print(f"📁 已导出: {master_csv.name}（{len(dated) + len(undated)} 条，其中日期>= {since} {len(dated)} 条、日期待定 {len(undated)} 条）")
    print(f"📁 已导出: {xlsx_path.name}（sheet: 全部 + 按国家 + 日期待定）")
    return {"csv": str(master_csv), "xlsx": str(xlsx_path), "dated": len(dated), "undated": len(undated)}
