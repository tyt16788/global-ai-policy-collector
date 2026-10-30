# -*- coding: utf-8 -*-
"""导出层：三份产物，分别服务三种用途。

1. `policy_master_*.csv`  —— 主表快照，**含全文列**，机器读（pandas/脚本），体积大。
2. `policy_master_browse_*.csv` —— **浏览版**，不含全文正文，只留"全文字数+网址"，
   Excel / WPS 可直接秒开（原始主表 65MB 且单元格上万字，Excel 会卡死或截断）。
3. `政策库视图_*.xlsx` —— Excel 视图，全文截断为预览列，含"概览"统计页 + 按国家分页。

⚠️ Excel 单元格上限 32767 字符，视图中的全文一律是截断预览；
   完整全文永远以 `data/policy_master.csv` 或 `policy_master_*.csv` 快照为准。
"""

from datetime import datetime

import re

import pandas as pd

from .config import load_config, resolve_path
from . import store

# Excel 视图里全文预览保留的字符数（够判断内容，又不至于把 xlsx 撑爆）
VIEW_FULLTEXT_CHARS = 800
TOP_COUNTRY_SHEETS = 15   # 分国家 sheet 只出前 15 个，其余合并为"其他国家"
# 浏览版 CSV 里摘要保留的字符数
BROWSE_SUMMARY_CHARS = 200
# 浏览版里内容指纹只留前 12 位，方便肉眼看
HASH_PREFIX = 12

COL_NAMES_ZH = {
    "title": "标题", "url": "网址", "summary": "摘要", "full_text": "全文预览(截断)",
    "published_date": "发布日期", "jurisdiction": "国家代码", "jurisdiction_name": "国家/地区",
    "level": "层级", "issuing_body": "发布机构", "policy_type": "政策类型", "language": "语言",
    "ai_tags": "AI标签", "source_name": "来源", "collection_method": "采集方式",
    "fulltext_status": "全文状态", "content_hash": "内容指纹",
}

# 浏览版列顺序：人看的顺序，不是库里的顺序
BROWSE_COL_NAMES_ZH = {
    "seq": "序号",
    "published_date": "发布日期",
    "year": "年份",
    "jurisdiction_name": "国家/地区",
    "level": "层级",
    "issuing_body": "发布机构",
    "policy_type": "政策类型",
    "title": "标题",
    "ai_tags": "AI标签",
    "full_text_chars": "全文字数",
    "fulltext_status": "全文状态",
    "source_name": "来源",
    "collection_method": "采集方式",
    "summary": "摘要(前200字)",
    "url": "网址",
    "content_hash": "内容指纹",
}
BROWSE_COLS = list(BROWSE_COL_NAMES_ZH.keys())


def _browse_frame(df: pd.DataFrame) -> pd.DataFrame:
    """把主表压成 Excel 友好的浏览版：砍掉全文正文，只留字数与溯源链接。"""
    v = pd.DataFrame(index=df.index)
    v["seq"] = range(1, len(df) + 1)
    for col in ("published_date", "jurisdiction_name", "level", "issuing_body",
                "policy_type", "title", "ai_tags", "source_name", "collection_method",
                "url"):
        v[col] = df[col].fillna("")
    v["year"] = df["published_date"].fillna("").str.slice(0, 4)
    v["full_text_chars"] = df["full_text"].fillna("").str.len()
    v["fulltext_status"] = df["fulltext_status"].fillna("")
    v["summary"] = df["summary"].fillna("").str.slice(0, BROWSE_SUMMARY_CHARS)
    v["content_hash"] = df["content_hash"].fillna("").str.slice(0, HASH_PREFIX)
    v = v[BROWSE_COLS].rename(columns=BROWSE_COL_NAMES_ZH)
    return v


def _overview_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Excel 视图的"概览"页：一眼看清总量、全文进度、国家与年份分布。"""
    total = len(df)
    ok = int((df["fulltext_status"].fillna("") == "ok").sum())
    rows = [
        ("导出时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("总条数", total),
        ("全文已抓取", f"{ok}（{ok / total * 100:.1f}%）" if total else "0"),
        ("全文失败/待重试", total - ok),
        ("", ""),
        ("—— 按国家/地区 ——", ""),
    ]
    for k, n in df["jurisdiction_name"].fillna("未知").value_counts().items():
        rows.append((k, n))
    rows += [("", ""), ("—— 按年份 ——", "")]
    for k, n in df["published_date"].fillna("").str.slice(0, 4).replace("", "未知").value_counts().sort_index().items():
        rows.append((k, n))
    rows += [("", ""), ("—— 按来源 ——", "")]
    for k, n in df["source_name"].fillna("未知").value_counts().items():
        rows.append((k, n))
    return pd.DataFrame(rows, columns=["项目", "数值"])


# ---------------- Excel 安全化 ----------------
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")


def sheet_name(name: str, used: set = None) -> str:
    """Excel 工作表名：去非法字符、限 31 字、去重。"""
    clean = _SHEET_BAD.sub("_", str(name)).strip()[:28] or "未知"
    if used is not None:
        base, i = clean, 2
        while clean in used:
            clean = f"{base[:26]}_{i}"
            i += 1
        used.add(clean)
    return clean


def _xlsx_safe(df: pd.DataFrame) -> pd.DataFrame:
    """去掉 Excel 不接受的控件字符并截断超长单元格，否则 openpyxl 报 IllegalCharacterError。"""
    try:
        from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    except Exception:  # noqa: BLE001
        ILLEGAL_CHARACTERS_RE = None
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            def _fix(v):
                s = "" if v is None else str(v)
                if ILLEGAL_CHARACTERS_RE is not None:
                    s = ILLEGAL_CHARACTERS_RE.sub("", s)
                return s[:32000]
            out[col] = out[col].map(_fix)
    return out


def run(since: str = "", xlsx: bool = False, browse: bool = True) -> dict:
    cfg = load_config()
    since = since or cfg["settings"]["start_date"]
    export_dir = resolve_path(cfg["settings"]["export_dir"])
    export_dir.mkdir(parents=True, exist_ok=True)

    df = store.load_master().copy()
    df = df[df["published_date"].isna() | (df["published_date"] == "") | (df["published_date"] >= since)]
    df = df.sort_values("published_date", ascending=False, ignore_index=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {"rows": len(df), "csv": "", "browse": "", "xlsx": ""}

    # ① 主表快照（含全文，机器读）
    csv_path = export_dir / f"policy_master_{stamp}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    out["csv"] = str(csv_path)
    print(f"📁 主表快照: {csv_path.name}（{len(df)} 条，{csv_path.stat().st_size / 1e6:.1f} MB，含全文）")

    # ② 浏览版 CSV（Excel/WPS 秒开）
    if browse:
        browse_path = export_dir / f"policy_master_browse_{stamp}.csv"
        _browse_frame(df).to_csv(browse_path, index=False, encoding="utf-8-sig")
        out["browse"] = str(browse_path)
        print(f"📁 浏览版 CSV: {browse_path.name}（{browse_path.stat().st_size / 1e6:.2f} MB，无全文正文，Excel 可直接打开）")

    # ③ Excel 视图（统计页 + 分国家页 + 全文截断预览）
    if xlsx:
        view = df.copy()
        for col in ("published_date", "jurisdiction", "jurisdiction_name", "level",
                    "issuing_body", "policy_type", "language", "ai_tags", "source_name",
                    "collection_method", "fulltext_status", "content_hash"):
            view[col] = view[col].fillna("")
        view = view.sort_values("published_date", ascending=False)
        cols = ["title", "url", "summary", "full_text", "published_date", "jurisdiction_name",
                "level", "issuing_body", "policy_type", "language", "ai_tags", "source_name",
                "collection_method", "fulltext_status"]
        view = view[cols].rename(columns=COL_NAMES_ZH)
        view["摘要"] = view["摘要"].astype(str).str.slice(0, 300)
        view["全文预览(截断)"] = view["全文预览(截断)"].astype(str).str.slice(0, VIEW_FULLTEXT_CHARS)

        xlsx_path = export_dir / f"政策库视图_{stamp}.xlsx"
        view = _xlsx_safe(view)
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            _xlsx_safe(_overview_frame(df)).to_excel(writer, sheet_name="概览", index=False)
            view.to_excel(writer, sheet_name="全部", index=False)
            used = {"概览", "全部"}
            counts = view["国家/地区"].value_counts()
            top = list(counts.index[:TOP_COUNTRY_SHEETS])
            for juris in top:
                grp = view[view["国家/地区"] == juris]
                grp.to_excel(writer, sheet_name=sheet_name(juris, used), index=False)
            rest = view[~view["国家/地区"].isin(top)]
            if len(rest):
                rest.to_excel(writer, sheet_name=sheet_name("其他国家", used), index=False)
        out["xlsx"] = str(xlsx_path)
        print(f"📁 Excel 视图: {xlsx_path.name}（{xlsx_path.stat().st_size / 1e6:.1f} MB，"
              f"全文截断为前 {VIEW_FULLTEXT_CHARS} 字预览）")

    return out


# ==================== 信号层（六大巨头博客）导出 ====================

LAYER_ZH = {"governance": "治理主张", "tech": "技术产品"}

SIGNAL_BROWSE_NAMES_ZH = {
    "seq": "序号",
    "published_date": "发布日期",
    "year": "年份",
    "company": "公司",
    "signal_layer": "信号层",
    "topic_tags": "主题标签",
    "ai_tags": "AI标签",
    "title": "标题",
    "full_text_chars": "全文字数",
    "fulltext_status": "全文状态",
    "source_name": "来源",
    "collection_method": "采集方式",
    "summary": "摘要(前200字)",
    "url": "网址",
    "content_hash": "内容指纹",
}
SIGNAL_BROWSE_COLS = list(SIGNAL_BROWSE_NAMES_ZH.keys())


def _signal_browse_frame(df: pd.DataFrame) -> pd.DataFrame:
    v = pd.DataFrame(index=df.index)
    v["seq"] = range(1, len(df) + 1)
    for col in ("published_date", "company", "topic_tags", "ai_tags", "title",
                "source_name", "collection_method", "url"):
        v[col] = df[col].fillna("")
    v["signal_layer"] = df["signal_layer"].fillna("").map(lambda x: LAYER_ZH.get(x, x))
    v["year"] = df["published_date"].fillna("").str.slice(0, 4)
    v["full_text_chars"] = df["full_text"].fillna("").str.len()
    v["fulltext_status"] = df["fulltext_status"].fillna("")
    v["summary"] = df["summary"].fillna("").str.slice(0, BROWSE_SUMMARY_CHARS)
    v["content_hash"] = df["content_hash"].fillna("").str.slice(0, HASH_PREFIX)
    return v[SIGNAL_BROWSE_COLS].rename(columns=SIGNAL_BROWSE_NAMES_ZH)


def _signal_overview_frame(df: pd.DataFrame) -> pd.DataFrame:
    total = len(df)
    ok = int((df["fulltext_status"].fillna("") == "ok").sum())
    rows = [
        ("导出时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("总条数", total),
        ("全文已抓取", f"{ok}（{ok / total * 100:.1f}%）" if total else "0"),
        ("全文失败/待重试", total - ok),
        ("", ""),
        ("—— 按公司 ——", ""),
    ]
    for k, n in df["company"].fillna("未知").value_counts().items():
        rows.append((k, n))
    rows += [("", ""), ("—— 按信号层 ——", "")]
    for k, n in df["signal_layer"].fillna("未知").replace("", "未知").value_counts().items():
        rows.append((LAYER_ZH.get(k, k), n))
    rows += [("", ""), ("—— 按年份 ——", "")]
    for k, n in df["published_date"].fillna("").str.slice(0, 4).replace("", "未知").value_counts().sort_index().items():
        rows.append((k, n))
    return pd.DataFrame(rows, columns=["项目", "数值"])


def run_signals(xlsx: bool = True) -> dict:
    """导出信号层三件套：全量快照、浏览版、Excel 视图（按公司分页）。"""
    from . import signal_store

    cfg = load_config()
    export_dir = resolve_path(cfg["settings"]["export_dir"])
    export_dir.mkdir(parents=True, exist_ok=True)
    df = signal_store.load().copy()
    if df.empty:
        print("⚠️ 信号表为空，跳过导出")
        return {"rows": 0}
    df = df.sort_values(["company", "published_date"], ascending=[True, False], ignore_index=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {"rows": len(df), "csv": "", "browse": "", "xlsx": ""}

    csv_path = export_dir / f"signal_master_{stamp}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    out["csv"] = str(csv_path)
    print(f"📁 信号表快照: {csv_path.name}（{len(df)} 条，{csv_path.stat().st_size / 1e6:.2f} MB）")

    browse_path = export_dir / f"signal_master_browse_{stamp}.csv"
    _signal_browse_frame(df).to_csv(browse_path, index=False, encoding="utf-8-sig")
    out["browse"] = str(browse_path)
    print(f"📁 信号浏览版: {browse_path.name}（{browse_path.stat().st_size / 1e6:.2f} MB，Excel 可直接打开）")

    if xlsx:
        view = df.copy()
        for col in ("published_date", "company", "topic_tags", "ai_tags", "source_name",
                    "collection_method", "fulltext_status", "language"):
            view[col] = view[col].fillna("")
        view["signal_layer"] = view["signal_layer"].fillna("").map(lambda x: LAYER_ZH.get(x, x))
        cols = ["title", "url", "summary", "full_text", "published_date", "company",
                "signal_layer", "topic_tags", "language", "ai_tags", "source_name",
                "collection_method", "fulltext_status"]
        view = view[cols].rename(columns=COL_NAMES_ZH).rename(
            columns={"company": "公司", "signal_layer": "信号层", "topic_tags": "主题标签"})
        view["摘要"] = view["摘要"].astype(str).str.slice(0, 300)
        view["全文预览(截断)"] = view["全文预览(截断)"].astype(str).str.slice(0, VIEW_FULLTEXT_CHARS)
        xlsx_path = export_dir / f"信号库视图_{stamp}.xlsx"
        view = _xlsx_safe(view)
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            _xlsx_safe(_signal_overview_frame(df)).to_excel(writer, sheet_name="概览", index=False)
            view.to_excel(writer, sheet_name="全部", index=False)
            used = {"概览", "全部"}
            for company, grp in view.groupby("公司", sort=False):
                grp.to_excel(writer, sheet_name=sheet_name(company, used), index=False)
        out["xlsx"] = str(xlsx_path)
        print(f"📁 信号库视图: {xlsx_path.name}（{xlsx_path.stat().st_size / 1e6:.1f} MB）")

    return out
