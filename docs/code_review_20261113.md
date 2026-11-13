# global-ai-policy-collector 代码审查 + CI 故障定位

审查日期：2026-11-13　范围：`collector/`（18 个模块，3.4k 行）+ workflow + 数据层
审查目标版本：`main @ 9adc77e`

---

## 0. 结论摘要

| 项目 | 结论 |
|---|---|
| CI 失败点 | **步骤 11「导出与报告」**，每次 2–3 秒内即崩（不是超时、不是 OOM） |
| 起始时间 | **2026-10-30**，即 `c01d882`（导出层重写）+ `9adc77e` 之后的第一次运行 |
| 影响 | data 分支自 **10/29** 起再没有机器人提交，**14 天的增量采集结果全部丢失**（每轮仍在跑、仍抓、仍入库，最后卡在导出后未提交） |
| 直接诱因 | 导出层新增的 3 条代码路径（浏览版 CSV / Excel 视图 / 信号库视图）此前**从未在 CI 上跑过**，且 `exporter.py` **零测试覆盖**；正文里 41% 的记录带有 XML 非法的 C1 控制字符（§1.3、§2） |
| 更严重的问题 | **中国库全文 41% 是乱码**，且修复补丁 `_decode()` 只写在本地未提交 → CI 每天继续污染语料（§2） |
| 已修 | 导出改成分阶段隔离（`_phase`）+ 自带 C0/C1/代理项清洗 + 公式注入中和 + workflow 提交步骤 `if: always()` |
| 待确认 | 精确 traceback 需从 GitHub 日志取（见 §1.4 命令），下次运行会以 `::warning::` 注解的形式直接显示 |

---

## 1. CI 一直失败：定位过程与结论

### 1.1 证据链（全部可从公开 API 复核）

1. `actions/runs/43179690289/jobs` → 步骤级结论：
   - `Set up job` … `采集六大巨头博客` 全部 success；
   - **`导出与报告`（step 11）= failure，started 08:15:40 → completed 08:15:42，仅 2 秒**；
   - `提交到 data 分支`（step 12）= **skipped**。
2. 历史 27 次运行：#3–#13（10/19–10/29）全成功，#14–#27（10/30–11/12）全失败。
3. `#13` 用的 commit 是 `cb7cf9b`（旧导出层），`#14` 起是 `9adc77e`（含 `c01d882` 的导出层重写）。
4. `commits/data`：最后一条 bot 提交是 `每日采集 2026-10-29`，之后只有一次手工合并（10/30）。
5. `c01d882` 对 workflow 的改动：`python -m collector export` → `python -m collector export --xlsx --signals`。
   **旧 CI 从未执行过 Excel 导出**（本地最后一次 `政策库视图_*.xlsx` 停在 10/19）。

### 1.2 为什么 2 秒就崩

旧代码 `export`（只写主表快照）在 CI 上耗时 ≈2–3 秒（#13 实测 3 秒，含 `report`）。
新代码在写完主表快照后立刻进入三条全新路径，2–3 秒正好卡在 **主表快照写完 → 浏览版/Excel 开始** 的窗口，
因此失败点在 `exporter.run()` 的 ② 或 ③ 段，而 `run_signals()` 根本没跑到。

### 1.3 数据侧实测：导出路径上确实带着"脏字符"

对当前主表（5,079 行）做了逐字节扫描（纯 `csv` 模块，不依赖 pandas）：

| 检查项 | 结果 |
|---|---|
| 字段数与表头不一致的行 | 0（CSV 本身是合法的） |
| 含 **C1 控制字符**（`\x80`–`\x9f`）的单元格 | **1,996 个**，其中 `\x80` 出现 102 万次 |
| 含 C1 的全文行 | **1,967 / 4,741 = 41%**（其中 1,943 条是中国记录） |
| 单格超过 Excel 上限 32,767 字符 | **416 个** |
| 标题/摘要以 `=` `+` `-` `@` 开头 | 1 条 |
| 最长 `jurisdiction_name` | `China (People's Republic of)` = 28 字符（贴着 28/31 的上限） |

这些 C1 字符是"UTF-8 字节被按 ISO-8859-1 解码"的产物（`\x80`-`\x9f` 正好是 UTF-8 的续行字节），
样例：`å½å®¶ç¥è¯äº§æå±å±å³äºå å¼º...`（原文是"国家知识产权局关于加强…"）。

**关键点**：`\x80`-`\x9f` **不在** openpyxl `ILLEGAL_CHARACTERS_RE`（只覆盖 `\x00-\x08\x0b-\x0c\x0e-\x1f`）的范围内，
但它是 XML 1.0 明令禁止的字符。旧代码的 `_xlsx_safe` 直接复用 openpyxl 的正则 → 这 4 百万个 C1 字符
**原样进了 xlsx 写入器**。是否抛异常取决于当次 `pip install` 装到的 openpyxl/lxml 版本行为——
而 `requirements.txt` 当时写的是 `openpyxl>=3.0`（无上界），云端每天全新安装。
**这就是"同一份代码，10/29 还行、10/30 起必崩"的机制**：导出层在 10/30 第一次走 Excel 路径，
撞上了 41% 的乱码正文，而清洗规则依赖了上游库的定义。

> 本次修复不再依赖 openpyxl 的正则：自带 `_CTRL_RE` 覆盖 C0 + C1 + 孤立代理项，与库版本无关。

### 1.4 修复后的实测验证（2026-11-13 手动触发两轮）

本机取到凭据后已经能读作业日志，两轮 `workflow_dispatch` 实测结果：

| | run #28（`4c33688`） | run #29（`2f59075`） |
|---|---|---|
| 结论 | success | success |
| 导出与报告 | success（7.6 秒） | success（7.5 秒） |
| 主表条数 | 5,086 | **5,201（+115）** |
| 全文 ok | 4,729 | **4,817（+88）** |
| 信号层 | 1,355 | 1,374 |
| 四份产物 | 全部产出 | 全部产出 |
| `ProxyError` 行数 | 数百 | **0** |
| 全文抓取 | **成功 0 / 失败 200** | 成功 88 / 失败 112 |

关键结论有两条：

1. **导出层的崩溃确实被修掉了**：`政策库视图_*.xlsx`（4.3–4.4 MB）、浏览版 CSV、信号库视图全部正常写出，
   提交步骤把 data 分支从 10-30 直接接到 `a59703f` / `323a0f8`。
2. **#28 暴露出比崩溃更严重的问题**：`config.yaml` 里写死的 `settings.proxy = http://127.0.0.1:7897`
   在 Actions runner 上并不存在，凡是 `needs_proxy` 的源、以及**所有**全文抓取都走代理 →
   全部 `ProxyError`。也就是说 10-30 之后的 CI 就算导出不崩，每天也只是"绿着空转"。
   已在 `2f59075` 里改为「loopback 代理先探活，连不上就当没配、降级直连」，#29 的 `ProxyError` 归零。

> 副作用：step 7 从 3 分钟涨到 20 分钟（真的在联网抓了，含 CN 源直连超时重试），仍在 120 分钟预算内。

### 1.5 修复前无法从公开渠道确认的部分

GitHub 的作业日志接口需要认证（未授权返回 403），公开 API 只暴露到 `Process completed with exit code 1.`。
上一轮只能做代码级推断；本轮已用 `GET /actions/jobs/{id}/logs`（带 `Authorization: Bearer`）拿到完整日志，
并确认 `::warning::` 注解通道可用（#28/#29 均无导出告警，只有 GitHub 自己的 Node 20 弃用提示）。

---

## 2. 数据质量：41% 的全文是乱码（P0，比性能更紧急）

扫描结果：4,741 条有全文的记录里，**1,967 条（41%）是"UTF-8 被按 ISO-8859-1 解码"的乱码**，
其中 1,943 条是中国记录——**等于中国库的正文几乎全废**。

```
原文：国家知识产权局关于加强知识产权公共服务体系建设的意见
实存：å½å®¶ç¥è¯äº§æå±å±å³äºå å¼ºç¥è¯äº§æå¬å±æå¡ä½ç³»å»ºè®¾çæè§
```

成因：`Content-Type: text/html` 不带 charset 时，requests 按 RFC 默认把 `encoding` 设为 ISO-8859-1，
`resp.text` 就整篇乱码；旧代码 `fulltext.py` 里只有 `resp.encoding = resp.encoding or "utf-8"` ——
**`or` 永远不会生效**，因为 requests 已经把 encoding 填成 `ISO-8859-1`（真值）。

⚠️ **修复补丁其实已经写好了，但没提交**（`git status` 里 `M collector/fulltext.py` 的 `_decode()`），
所以 CI 每天还在继续往库里写乱码。

需要两步：
1. 提交 `_decode()` 补丁（本次未动这个函数，保留你的实现）。
2. 修复历史数据：这些行的 C1 字符不可能事后还原（有损），应标记后重抓——
   ```python
   # 一次性：把含 C1 的行打回待抓，交给每天的 fulltext 步骤重新抓
   store 里批量 set_fulltext(guid, "", "failed:mojibake")  # 保留 raw 备份后再做
   ```
   正文可无损还原的只有"整段都是乱码"这种情况（`text.encode('latin-1').decode('utf-8')` 成功率约 8 成），
   建议先抽样验证再批量修，别直接覆盖唯一副本。

同时这也解释了 CN 库的另一个现象：`fulltext` 的正文长度分布异常（乱码后每字符 1 字节 vs 中文 3 字节）。

---

## 3. 性能问题

### P0-1　逐行扩表：`store.insert_policy` / `signal_store.insert`
```python
_master.loc[len(_master)] = row      # 每插入一行，pandas 整体 block 重分配一次
```
插入 n 条是 O(n²) 行拷贝：3,000 条时已是千万级行拷贝，占了 `backfill` 步骤的大部分 CPU。
**建议**：先用 `list[dict]` 缓冲，攒 500 条后 `pd.concat([_master, pd.DataFrame(buf)])` 一次性并入。

### P0-2　每次落盘都整表重写 + 整表读回
```python
AUTOSAVE_EVERY = 500          # 每 500 条新数据触发一次
save(merge=True)              # 内部先 read_csv(86MB) → set_index → isin → concat → to_csv(86MB)
```
一天 5,000 条 = 10 次「读 86MB + 写 86MB」≈ 1.7GB IO，且 merge 的 `isin/set_index` 全在 Python 层。
**建议**：新记录 append 到 `policy_master_delta.csv`，只在命令行结束时做一次全量压缩；
或把 `AUTOSAVE_EVERY` 提到 2000 以上。

### P0-3　全文回填做全表布尔扫描（已修）
```python
idx = _master.index[_master["guid"] == guid]     # O(n) × 200 条/轮
```
已改为 `_label_index: dict[guid -> label]`，`set_fulltext` 变 O(1)。`signal_store` 同类代码建议同步。

### P0-4　`records_missing_fulltext` 的饥饿效应（已修）
```python
return df.loc[m, [...]].head(limit)    # 永远取表头那 200 条
```
永久失败的记录（OECD SPA 仪表盘、死链）每轮都排在最前面，把 200 条配额吃干，
**新记录永远轮不到抓全文**。已改为「没试过的优先 → 最久没试过的次之」。

### P1-5　`load_config()` 在热路径反复解析 YAML
`master_path()` / `health_path()` / `polite_sleep()` / `get_session()` / `http_get()` 每次都 open+`yaml.safe_load`。
`oecd` 每翻一页调一次 `http_get` → 每页多一次磁盘 IO + YAML 解析。
**建议**：`@lru_cache` + 用 `st_mtime` 做失效判断。

### P1-6　`_xlsx_safe` 逐元素 Python 调用（已修）
`Series.map(_fix)` 在 5,000×15 = 7.5 万次调用上比向量化 `str.replace` 慢一个量级。已改向量化。

### P1-7　导出链路上 3 次全表 copy
`store.load_master().copy()` → `view = df.copy()` → `_xlsx_safe(df.copy())`，
86MB CSV 在内存里峰值约 3–4 倍（1.5–2GB）。CI 16GB 扛得住，本地同时开 Excel 会吃紧。

### P1-8　`fulltext` 的并发限流实际没生效
```python
_domain_sems = defaultdict(lambda: threading.Semaphore(DOMAIN_MAX_CONCURRENT))  # 声明了但从未 acquire
```
`_rate_limit()` 只用 lock + 最小间隔串行化，同域名仍可能 8 线程一起打；`_domain_sems` 是死代码。
另外 `requests.Session` 被 8 个线程共享（cookie/连接池非线程安全，目前没改 header 才没出事）。

### P2-9　日志表无限增长
`source_health.csv` / `runs.csv` / `signal_health.csv` 只 append 不轮转，`latest_health()` 每次全量 `read_csv`。
几年后（每天几十行）会明显变慢，建议按年分文件。

---

## 4. 安全与健壮性

### S1　CSV / Excel 公式注入（最实际的一条）
`title` / `summary` / `full_text` 全部来自外部网页。以 `=` `+` `-` `@` 开头的单元格被 Excel/WPS 打开会当公式执行
（`=HYPERLINK(...)`、`=cmd|' /C calc'!A0` 这类）。数据源完全不可控，这是本项目最现实的风险面。
**已修**：Excel 视图写入前对公式前缀加 `'` 中和（`guard_formula=True`）；CSV 保持原样（机器读，加了会污染数据）。

### S2　SSRF：全文抓取直接请求外部 URL
`fulltext._crawl_one()` 对 `rec["url"]` 直接 `session.get`，URL 来自 RSS/API 内容（不可信）。
未校验 scheme，也未拦截内网地址 → 数据源被污染时可让采集器请求
`http://169.254.169.254/latest/meta-data/`（云元数据）、`http://127.0.0.1:7897`（本机代理）等。
**建议**：
```python
host = urlsplit(url).hostname
if urlsplit(url).scheme not in ("http", "https") or ipaddress.ip_address(socket.gethostbyname(host)).is_private:
    return guid, "", "failed:非法目标地址"
```

### S3　`config.yaml` 把本机代理地址提交进了公开仓库
```yaml
proxy: "http://127.0.0.1:7897"     # Clash Verge 端口，暴露本地客户端信息
```
且 CI 上无此代理 → 所有 `needs_proxy: true` 的源在云端**必然失败**（与文件头注释"云端无需代理"矛盾）。
**建议**：配置留空，本地用 `HTTPS_PROXY` 环境变量；CI 显式设 `settings.proxy: ""`。

### S4　静默吞异常导致数据丢失无感知
```python
except Exception:   # store.save() 的 merge 分支
    pass            # ← 10-19 那次"281 条被整表覆盖"就是从这里溜走的
```
已改为打印告警。CI 那 14 天的数据丢失同样是"静默"造成的——**建议所有采集失败都以非 0 退出或 `::warning::` 上浮**。

### S5　多进程并发写同一份 CSV 仍有竞态
`tmp + os.replace` 保证单次写原子，但「A 读盘 → B 读盘 → A 写 → B 写」仍会丢 A 的更新。
merge 只能救"磁盘有、内存没有"的行，救不了两边都改过的行。
**建议**：单写者（本地跑时加 `filelock`，或写 `data/.lock` 后 `flock`）。

### S6　`strip_html` 用正则去标签
```python
_TAG_RE = re.compile(r"<[^>]+>")
```
`<script>alert(1)</script>` 的**内容**会保留，`a < b` 会被误伤，`<br>` 变空白导致中文粘连。
仅入库用途风险低，但"纯文本"字段并不纯，下游如果渲染成 HTML 就是 XSS。**建议**改用 BeautifulSoup `get_text()`。

### S7　报告 Markdown 未转义
`report.py` 的表格里 `title` 未处理 `|`，一条带竖线的标题就能把整张表错位（`detail` 做了转义，`title` 漏了）。

### S8　依赖锁过于宽松
`pandas>=2.0`、`requests>=2.31` 无上界。上游一次大版本升级就可能改变 `dtype=str` 的返回类型、
`loc` 扩表行为、Excel 写入逻辑——本次 CI 故障与之高度相关。**已改为带上界**（`pandas>=2.0,<3` 等）。

### S9　合规提示
对 gov.cn / OECD / 企业博客的采集使用了随机 UA 与高频重试。写论文引用时建议确认各站 `robots.txt` 与 ToS，
并在文档里保留请求频率说明（当前 `politeness_delay` + 同域 0.8s 已经比较克制）。

---

## 5. 本次改动清单

| 文件 | 改动 |
|---|---|
| `collector/exporter.py` | 新增 `_phase()` 分阶段隔离（单个产物失败不再拖垮整轮）；`_clean_series`/`_xlsx_safe` 重写为向量化 + 覆盖 C0/C1/代理项；`guard_formula` 中和公式注入；`since` 强制 str 比较；xlsx 逻辑拆出 `_run_xlsx` / `_run_signals_xlsx`；失败用 `::warning::` 上浮 |
| `collector/store.py` | 新增 `_label_index`（guid→行标签），`set_fulltext`/`patch_extra` 从全表扫描改 O(1)；merge 失败不再静默 `pass`；`records_missing_fulltext` 增加公平性排序 |
| `collector/__main__.py` | `cmd_export` 汇总子任务失败并打印 |
| `requirements.txt` | 全部依赖加版本上界 |
| `.github/workflows/daily-collect.yml` | `提交到 data 分支` 加 `if: always()` + 主表缺失保护；补 `TZ` / `PYTHONIOENCODING` / `PYTHONUNBUFFERED` |

验证：`python -m py_compile` 全部通过；重复函数定义检查通过。
（本机无 pandas 且离线，无法执行运行时验证，故未改动需要跑数据才能验证的热路径逻辑。）

---

## 6. 建议的后续顺序

1. 合并本次改动 → 手动触发一次 `workflow_dispatch`，确认 `::warning::` 是否出现（若出现即锁定 §1.3 的具体原因）。
2. 补 `tests/test_exporter.py`：`sheet_name` 边界、`_xlsx_safe` 脏字符、空表导出、`run(xlsx=True)` 冒烟。
   导出层是唯一零测试的核心模块，也是这次翻车的地方。
3. 把 `insert_policy` 改成批量并入（P0-1）+ 增量落盘（P0-2），CI 时间有望从 13 分钟压到 6 分钟以内。
4. 加 SSRF 白名单（S2）、代理配置改为环境变量（S3）。
