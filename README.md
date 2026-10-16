# 全球 AI 政策采集系统（global-ai-policy-collector）

为"多源新兴技术主题识别"研究（Stage E 政策佐证）采集全球主要经济体的 AI 相关政策。
历史数据用官方检索 API 回填（2023-01-01 起），日常用 RSS/Atom 增量更新，统一入 SQLite 库去重后导出。

**部署形态**：GitHub 私有仓库，`main` 分支只放代码，每日 GitHub Actions 自动增量采集并把数据提交到 `data` 分支；本地可随时运行同一套命令。

## 目录结构

```
rss_spider/
├── collector/            # 采集系统代码主体
│   ├── config.yaml       # ★ 所有数据源、代理、参数都在这里改
│   ├── config.py         # 配置加载
│   ├── normalize.py      # URL归一化 / 去重键 / 日期解析 / AI打标
│   ├── fetcher.py        # 带重试+代理的 HTTP 抓取器
│   ├── db.py             # SQLite 数据层（主表/源健康/运行日志）
│   ├── exporter.py       # 导出 CSV + Excel（按国家分 sheet）
│   ├── report.py         # 采集报告（统计/健康/疑似重复）
│   ├── fulltext.py       # 可选：抓政策正文存 TXT
│   └── sources/          # 数据源模块
│       ├── rss_source.py         # RSS/Atom 增量（中/美/欧/英）
│       ├── govcn.py              # gov.cn 国务院政策文件库（回填，直连）
│       ├── federal_register.py   # 美国 Federal Register API（回填，直连）
│       └── oecd.py               # OECD.AI 政策观察站（回填，墙外直连）
├── .github/workflows/daily-collect.yml  # 每日自动采集 workflow
├── tests/                # 单元测试（纯函数，不联网）
```

## 分支说明

| 分支 | 内容 | 谁在更新 |
|---|---|---|
| `main` | 代码 + workflow + 文档（**无任何采集数据**） | 人工 |
| `data` | `data/policy.sqlite` 主库 + 最近几轮 `exports/` 产物 | Actions 每日自动 |

> ⚠️ SQLite 是二进制单文件，git 无法合并、只有"最后提交者胜"。请约定**唯一写入方**：
> 默认让云端 Actions 每日采集（推荐，本地只读取导出结果）；若要在本地跑采集，
> 先从 `data` 分支拉最新库覆盖本地，跑完再推回 `data` 分支（见下文"本地与云端同步"）。

## 本地使用

```bash
pip install -r requirements.txt

python -m collector check            # 源连通性检查（不入库）
python -m collector update           # RSS 增量采集
python -m collector backfill all     # 历史回填（govcn / federal / oecd 可单独跑）
python -m collector export           # 导出 CSV + Excel
python -m collector report           # 生成采集报告
python -m collector fulltext --limit 200   # 可选：补抓政策正文存 TXT

python -m unittest discover tests -v # 单元测试
```

## GitHub Actions 自动采集

- **触发**：每天北京时间 09:00 自动运行（`cron: 0 1 * * *` UTC），也可在 Actions 页面手动触发（workflow_dispatch）。
- **流程**：恢复 data 分支的数据库 → RSS 增量 → gov.cn / Federal Register / OECD 回填 → 导出 + 报告 → 提交回 `data` 分支。
- **代理**：GitHub 云端在墙外，OECD/英国等源无需代理；config.yaml 的 `proxy` 在云端保持为空即可。
- **注意**：`config.yaml` 会随代码公开到仓库（私有仓库仅自己可见）。如以后填入带密码的代理地址，请改用仓库 Secrets + 环境变量，不要硬编码。

## 本地与云端同步（SQLite 单文件，二选一写入）

```bash
# 场景 A（推荐）：云端为主，本地只想看最新数据
git fetch origin data
git checkout origin/data -- data/policy.sqlite exports/   # 覆盖到本地工作区

# 场景 B：本地跑了一次采集，要把结果推上云端
git fetch origin data
git checkout origin/data -- data/policy.sqlite            # 先同步云端最新库
python -m collector update ...                            # 本地采集（在云端库基础上增量）
git checkout data && git add -f data/policy.sqlite && git commit -m "本地采集" && git push origin data
git checkout main
```

## 数据说明

主表 `policies` 每条记录：标题 / 网址（原件入口）/ 摘要 / 发布日期 / 国家代码 / 层级 /
发布机构 / 政策类型 / 语言 / AI标签 / 来源 / 采集方式 / 全文路径 / 内容指纹。

- **去重**：URL 归一化后哈希为主键（guid）；同"国家+标题+日期"不同网址的记录保留，
  但在报告的"疑似重复"一节列出供人工裁决（多为美国多机构联合发文）。
- **时间窗**：早于 `settings.start_date`（默认 2023-01-01）不入库；无日期记录保留并导出到"日期待定"。
- **AI 标签只标注不过滤**：gov.cn 全文检索会命中大量仅提及 AI 的政策，用 `AI标签` 列区分，
  后续做主题佐证匹配时自行决定筛选口径。
- **政策类型**映射：gov.cn 统一为"政策文件"（文号/分类在 extra_json）；Federal Register 分
  最终法规/法规草案/通知公告/总统文件；OECD 分法规(有约束力)/指南战略(无约束力)。

## 数据源一览

| 源 | 方式 | 覆盖 | 本地网络 | Actions 云端 |
|---|---|---|---|---|
| gov.cn 国务院政策文件库 | 官方检索 API 回填 | 中国中央（国务院+部委） | 直连 | 直连 |
| Federal Register | 官方 API 回填 | 美国行政部门全量文件 | 直连 | 直连 |
| OECD.AI 政策观察站 | 站点 JSON 接口 | 全球 70+ 国家（默认筛选 10 国） | 需代理 | 直连 |
| 工信部 / gov-rss聚合 / 统计局 | RSS | 中国 | 直连 | 直连 |
| NIST / EU AI Act Tracker | RSS | 美国 / 欧盟 | 直连 | 直连 |
| 英国 DSIT、欧盟数字战略 | Atom/RSS | 英国 / 欧盟 | 需代理 | 直连 |

> OECD 端点若失效：浏览器打开 https://oecd.ai/en/dashboards/policy-initiatives → F12 Network →
> 找返回 JSON 的请求 → 更新 `collector/config.yaml` 的 `oecd.endpoint`。

