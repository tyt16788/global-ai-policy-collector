# 全球 AI 政策采集系统（global-ai-policy-collector）

为"多源新兴技术主题识别"研究（Stage E 政策佐证）采集全球主要经济体的 AI 相关政策：
历史数据用官方检索 API 回填（2023-01-01 起），日常用 RSS/Atom 增量更新，
统一进**单一 CSV 主表**（含全文列）去重后导出。

**部署形态**：GitHub 私有仓库，`main` 分支只放代码，每日 GitHub Actions 自动增量采集并把数据提交到 `data` 分支；本地可随时运行同一套命令。

## 目录结构

```
rss_spider/
├── collector/            # 采集系统代码主体
│   ├── config.yaml       # ★ 所有数据源、代理、参数都在这里改
│   ├── config.py         # 配置加载
│   ├── normalize.py      # URL归一化 / 去重键 / 日期解析 / AI打标
│   ├── fetcher.py        # 带重试+代理的 HTTP 抓取器
│   ├── store.py          # CSV 存储层（主表读写、去重、健康日志，无 SQL）
│   ├── fulltext.py       # 全文抓取（多线程，按域名限速）
│   ├── exporter.py       # 导出主表快照 / Excel 浏览视图
│   ├── report.py         # 采集报告（统计/健康/疑似重复）
│   └── sources/          # 数据源模块
│       ├── rss_source.py         # RSS/Atom 增量（中/美/欧/英）
│       ├── govcn.py              # gov.cn 国务院政策文件库（回填，直连）
│       ├── federal_register.py   # 美国 Federal Register API（回填，直连）
│       └── oecd.py               # OECD.AI 政策观察站（回填，墙外直连）
├── .github/workflows/daily-collect.yml  # 每日自动采集 workflow
├── tests/                # 单元测试（纯函数，不联网）
├── data/                 # 【不入 main 分支】policy_master.csv 主表 + 健康日志
├── exports/              # 【不入 main 分支】导出的 CSV 快照 / Excel 视图 / 报告
└── 垃圾桶/               # 【不入库】旧版本脚本与历史产物
```

## 分支说明

| 分支 | 内容 | 谁在更新 |
|---|---|---|
| `main` | 代码 + workflow + 文档（**无任何采集数据**） | 人工 |
| `data` | `data/policy_master.csv` 主表（含全文）+ 日志 + 最近几轮 `exports/` | Actions 每日自动 |

> CSV 是文本文件，git 可正常合并；但同一单元格的两处改动仍会冲突，请保持**唯一写入方**
> 约定：默认让云端 Actions 每日采集（推荐）；本地采集前先拉取 data 分支最新主表（见下文同步）。

## 主表结构（data/policy_master.csv）

每行一条政策，列：`guid`（去重键）、`title`、`url`（原件入口）、`summary`（接口/RSS 自带摘要）、
**`full_text`（原文全文）**、`jurisdiction`/`jurisdiction_name`（国家）、`level`（层级）、
`issuing_body`（发布机构）、`policy_type`、`language`、`published_date`、`source_name`、
`collection_method`、`ai_tags`（AI 关键词标注，只标注不过滤）、`fulltext_status`（ok/failed:原因）、
`content_hash`（内容指纹）、`extra_json`（文号/分类等扩展）、`collected_at`、`fulltext_crawled_at`。

- **去重**：URL 归一化后哈希为 guid；同"国家+标题+日期"不同网址的保留，在报告"疑似重复"列出。
- **时间窗**：早于 `settings.start_date`（默认 2023-01-01）不入库；无日期的保留待定。
- **全文获取**：美国走 Federal Register 官方纯文本接口；gov.cn 爬 `#UCAP-CONTENT` 正文容器；
  其余爬链接页启发式抽取。失败的记录每轮自动重试。
- ⚠️ **不要用 Excel 打开主表后直接保存**：Excel 单元格上限 32767 字符会静默截断全文。
  浏览请用 pandas/WPS，或用 `export --xlsx` 生成的截断预览视图。

## 本地使用

```bash
pip install -r requirements.txt

python -m collector check            # 源连通性检查（不入库）
python -m collector update           # RSS 增量采集
python -m collector backfill all     # 历史回填（govcn / federal / oecd 可单独跑）
python -m collector fulltext         # 抓取缺失全文（失败自动重试）
python -m collector export           # 导出主表 CSV 快照
python -m collector export --xlsx    # 同时导出 Excel 视图（全文截断预览）
python -m collector report           # 生成采集报告

python -m unittest discover tests -v # 单元测试
```

## GitHub Actions 自动采集

- **触发**：每天北京时间 09:00（`cron: 0 1 * * *` UTC），可手动触发（workflow_dispatch）。
- **流程**：从 data 分支恢复主表 → RSS 增量 → gov.cn / Federal Register / OECD 回填 →
  抓取新增记录全文 → 导出 + 报告 → 提交回 `data` 分支。
- **代理**：云端在墙外，OECD/英国等源无需代理；`config.yaml` 的 `proxy` 保持为空。
- **注意**：如以后在 config 填入带密码的代理地址，请改用仓库 Secrets，不要硬编码。

## 本地与云端同步

```bash
# 场景 A（推荐）：云端为主，本地更新到最新数据
git fetch origin data
git checkout origin/data -- data/policy_master.csv data/source_health.csv data/runs.csv

# 场景 B：本地跑了采集，推回云端
git fetch origin data
git checkout origin/data -- data/policy_master.csv   # 先同步云端最新
python -m collector update && python -m collector fulltext   # 在云端库基础上增量
git checkout data && git add -f data/ exports/ && git commit -m "本地采集" && git push origin data
git checkout main
```

## 数据源一览

| 源 | 方式 | 覆盖 | 本地网络 | Actions 云端 |
|---|---|---|---|---|
| gov.cn 国务院政策文件库 | 官方检索 API 回填 | 中国中央（国务院+部委） | 直连 | 直连 |
| Federal Register | 官方 API + 官方全文接口 | 美国行政部门全量文件 | 直连 | 直连 |
| OECD.AI 政策观察站 | 站点 JSON 接口 | 全球 70+ 国家（默认筛选 10 国） | 需代理 | 直连 |
| 工信部 / gov-rss聚合 / 统计局 | RSS | 中国 | 直连 | 直连 |
| NIST / EU AI Act Tracker | RSS | 美国 / 欧盟 | 直连 | 直连 |
| 英国 DSIT、欧盟数字战略 | Atom/RSS | 英国 / 欧盟 | 需代理 | 直连 |

> OECD 端点若失效：浏览器打开 https://oecd.ai/en/dashboards/policy-initiatives → F12 Network →
> 找返回 JSON 的请求 → 更新 `collector/config.yaml` 的 `oecd.endpoint`。

## 历史版本

`垃圾桶/` 下是 1.py–4.py 的迭代版本、旧 notebook 与旧导出，仅本地存档，不进入仓库。
