# -*- coding: utf-8 -*-
"""导出：主表 CSV 快照（含全文列）；可选 Excel 浏览视图（全文截断为预览列）。

⚠️ Excel 单元格上限 32767 字符，因此 Excel 视图中的全文为截断预览；
完整全文永远以 exports/policy_master_*.csv（或 data/policy_master.csv）为准。
"""

from datetime import datetime

import pandas as pd

from .config import load_config, resolve_path
from . import store

VIEW_FULLTEXT_CHARS = 3000
COL_NAMES_ZH = {
    "title": "标题", "url": "网址", "summary": "摘要", "full_text": "全文预览(截断)",
    "published_date": "发布日期", "jurisdiction": "国家代码", "jurisdiction_name": "国家/地区",
    "level": "层级", "issuing_body": "发布机构", "policy_type": "政策类型", "language": "语言",
    "ai_tags": "AI标签", "source_name": "来源", "collection_method": "采集方式",
    "fulltext_status": "全文状态", "content_hash": "内容指纹",
}
VIEW_COLS = ["title", "url", "summary", "full_text", "published_date", "jurisdiction",
             "jurisdiction_name", "level", "issuing_body", "policy_type", "language",
             "ai_tags", "source_name", "collection_method", "fulltext_status"]


def run(since: str = "", xlsx: bool = False) -> dict:
    cfg = load_config()
    since = since or cfg["settings"]["start_date"]
    export_dir = resolve_path(cfg["settings"]["export_dir"])
    export_dir.mkdir(parents=True, exist_ok=True)
    df = store.load_master().copy()
    df = df[df["published_date"].isna() | (df["published_date"] == "") | (df["published_date"] >= since)]
    df = df.sort_values("published_date", ascending=False, ignore_index=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = export_dir / f"policy_master_{stamp}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    size_mb = csv_path.stat().st_size / 1e6
    print(f"📁 已导出主表快照: {csv_path.name}（{len(df)} 条，{size_mb:.1f} MB，含全文列）")

    xlsx_path = ""
    if xlsx:
        view = df[VIEW_COLS].copy()
        view["full_text"] = view["full_text"].str.slice(0, VIEW_FULLTEXT_CHARS)
        view = view.rename(columns=COL_NAMES_ZH)
        xlsx_path = export_dir / f"政策库视图_{stamp}.xlsx"
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            view.to_excel(writer, sheet_name="全部", index=False)
            for juris, grp in view.groupby("国家代码", sort=False):
                grp.to_excel(writer, sheet_name=str(juris)[:31] or "未知", index=False)
        print(f"📁 已导出 Excel 视图: {xlsx_path.name}（全文截断为前 {VIEW_FULLTEXT_CHARS} 字预览）")

    return {"csv": str(csv_path), "xlsx": xlsx_path, "rows": len(df)}
