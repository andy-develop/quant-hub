# handoff

域移交文档索引：

- 短线域（quant-lab）：HANDOFF.md 见原仓 /tmp/qh 检出（data 口径、除权修复、HSK 迁移史）
- ETF 域（red-dividend-strategy）：v7.x 版本史与 HSK 部署细节见原仓
- 个性化选股域（stock-factor-engine）：因子口径见原仓 scripts/compute_factors.py

---

## 数据链阶段A/B 交接（2026-09-13）

依据《quant-hub-合并方案》§2.4（写库时序 16:35 data-index → 16:40 data-stock → 16:55 data-etf → 17:00 契约+verify）、grill-me 五轮决策（个股 3 年/指数 10 年/日级增量顺手删/摘要入 state/）实施。

### 数据仓（andy-develop/quant-hub-data）已上传 workflow

| 文件 | 触发 | 说明 |
|---|---|---|
| `data-index.yml` | cron 16:35 | 指数全史/增量（h 区 H2 闸门、幂等提交） |
| `data-stock-incr.yml` | cron `40 8 * * 1-5`（北京 16:40） | 个股日级增量：腾讯快照+除权检测+hfq 折算+ifzq 修复；`--expire` 730 交易日顺手删旧 |
| `data-etf-incr.yml` | cron `55 8 * * 1-5`（北京 16:55） | 中证指数（H20269/H30269/H00300/000300，asset=etf，2430 日）增量 + 周/月派生 |

### 代码仓新增脚本

- `tools/data_pipeline/stock_incr.py`：gate → 幂等（`_incr/YYYYMMDD` 存在 **或 `_sealed_has_day`** 封存分区已含 target_day → 跳过）→ 腾讯快照 60/批 → 覆盖率门禁（<80% 红中止/<95% 黄）→ 除权检测（昨收 |Δ|>0.5% + 保险丝 max(50,0.3n)）→ ifzq 分页修复 + qfq×K hfq 折算 → `write_incremental` → fixup 分区 → `_expire` → runlog
- `tools/data_pipeline/etf_incr.py`：复用 `csindex.py` 口径（`ingest_daily_increment/derive_period/freshness_gate/bars_to_frame`），按天分片写 `_incr/YYYYMMDD`（幻影行以 `calendar.is_trading_day` 过滤），重物化 weekly/monthly，末尾 expire 2430 交易日

### 关键约定（勿破坏）

1. **幂等语义**：stock 增量跳过条件 = `_incr/YYYYMMDD` 存在 **或** 封存分区已含该日（首灌封存已到 target_day 时不得重复入库）
2. **老仓 hfq 口径**：hfq = qfq×K，K=库内最后 hfq close / 重拉 qfq 同日 close（后复权锚定），除权日修复优先
3. **写入 API**：`common/store/writer.py::write_incremental / seal_partition / expire_partitions`（封存时删 _incr 日分片；expire 前归档 `_archive/`）
4. **wflow 模板**：双仓 checkout（本仓+`code/`）、`PYTHONPATH: code`、H2 交易日闸门（schedule 才跑）、`concurrency: group: data-commit`、幂等提交（`git diff --cached --quiet` 跳过）
5. **push 数据仓方式**（本地）：`GH_PAT=$(gh auth token) GIT_ASKPASS=/tmp/askpass.sh GIT_TERMINAL_PROMPT=0 git -c credential.helper= -c http.proxy=socks5h://127.0.0.1:7897 push origin integration:main`

### 验证记录

- 阶段A：个股首灌（run 34694306983）、ETF 首载（run 34696393009）、sh000852 回补（run 34708789967）verify 全绿
- 阶段B：data-stock-incr（run 34734901170）/ data-etf-incr（run 34734910940）dispatch 均 success，执行路径为合法幂等跳过（stock：封存已含 09-11；etf：无增量窗口），gate/幂等/提交链在 CI 可跑通
- 本地：`pytest tests/` 全绿（含 `stats.ok` 回填修复后 27 passed）；腾讯快照真实抓取冒烟 59/60（`FetchStats.ok` 合并仓遗留死字段已回填 = `len(rows)`，commit `5620ca3`）

### 遗留事项

- 真实抓取路径待下一交易日 schedule 首跑验证（本地冒烟已过，CI 首次真实跑未发生）
- `FetchStats` parse 级丢弃明细（short_format/parse_error 等）在 `TencentSnapshotVendor.to_vendor._fetch` 局部 stats 中未回流外层 runlog（`ok` 已修复，明细拆分待后续）
- `tools/verify.py::check_derived` 只覆盖 `index_` 前缀 manifest，`etf_csindex_` 派生不在 verify 覆盖内（阶段F 前需补）

---

## Phase 5/6 适配移植交接（2026-09-12）

依据《quant-hub-合并方案》§2.4/§3.1/§9.5/§9.7/§10.2-10.4 将本地 Phase 5/6 工具适配到本仓真实 API 并接入 CI，全部已验证。

### 新增/修改文件

| 文件 | 说明 |
|---|---|
| `tools/retention.py` | 周度封存+过期+体积报告。调用 `common.store.writer.expire_partitions / read_manifest / write_manifest`（**不是** `expire/update_manifest`，后者不存在）；`_size_report()` 对缺失 `data/` 目录容错返回 `{}`（CI 数据仓未挂载时防 FileNotFoundError） |
| `tools/runlog.py` | §3.1 runlog 台账统一写入：`write_runlog(state_root, domain, ...)` → `state/<domain>/runlog/YYYY-MM-DD.json`（幂等覆盖），`read_runlogs / recent_days` 供巡检用 |
| `tools/repo_health.py` | 周巡检：最近 5 交易日 runlog、payload 体积（读 `state/payload/{domain}.{variant}.json` 最新份）、数据仓工作树体积（700MB warn / 900MB error）。**域映射**：`shortterm→quant-lab`、`etf→etf`、`selected→stock`（payload 文件名的 domain 与逻辑域不同） |
| `tools/shadow_diff.py` | 新增公开 `extract_kpis(domain, payloads)`（短线 24 项 / ETF 18 项 / selected 无），私有 `_series_sharpe`/`_max_drawdown`/`_shortterm_line_kpis` |
| `tools/check_docs.py` | AUTO-KPI 生成/校验。payload 路径改为 `state/payload/{domain}.{variant}.json` glob（原 `state/<d>/payload/payload.json` 不存在）；`--check-only` 供 CI 校验 |
| `README.md` | 插入 `<!-- AUTO-KPI:START ... -->` 锚点（严禁手改，`check_docs.py` 刷新） |

### 接入的流水线

- `data-retention.yml`：cron `0 19 * * 6`（周六=周日 03:00 CST）；跑 retention + 写 `state/data-retention/runlog/`；job env `GH_TOKEN: ${{ github.token }}`（gh release 用）
- `repo-health.yml`：周一 01:01 UTC；`grep -q '"healthy": *true'`（**不能只 grep "healthy"**，false 也含该子串）；多份报告用 Python `glob + files[-1]` 单文件读取（**不能 cat 拼接**，json.load 会崩）；告警 issue 按 title 前缀去重；job env `GH_TOKEN: ${{ github.token }}`
- `strategy-pm.yml`：跑完短线计算链写 `state/shortterm/runlog/`；提交范围由 `state/payload` 扩为 `state/`（payload+runlog 一起）
- `build-publish.yml`：payload 生成后 `check_docs.py --check-only`（continue-on-error，仅 warning）；HSK 发布后写 `state/etf/`、`state/selected/` runlog

### 关键约定（勿破坏）

1. **payload 域映射**：payload 文件名用 `quant-lab`（短线）/`etf`/`stock`（选股），逻辑域是 `shortterm`/`etf`/`selected` —— 两处映射表：`tools/check_docs.py::_DOMAIN_MAP`、`tools/repo_health.py::PAYLOAD_DOMAIN`
2. **runlog 目录**：逻辑域目录名 `state/<domain>/runlog/`（`write_runlog` 语义），data-retention 域即 `state/data-retention/runlog/`
3. **retention 封存**：hfq 冻结、sealed 分区不可重写；`expire_partitions(..., archive=True)` 内置归档兜底
4. **AUTO-KPI 锚点**：`<!-- AUTO-KPI:START (check_docs.py 生成, 严禁手改) ... AUTO-KPI:END -->`，CI `--check-only` 不一致即 warning

### 验证记录

- 本地：`pytest tests/` 284 passed / 3 skipped；retention dry-run、repo_health、check_docs（刷新+`--check-only`）全绿
- 远程（push 至 `origin/main`）：strategy-pm ✓ · build-publish 手动 ✓ · data-retention ✓ · repo-health ✓（首轮因 GH_TOKEN 缺失失败，`6f5db99` 补 `env: GH_TOKEN: ${{ github.token }}` 后成功）
- HSK_API_KEY / DATA_REPO_TOKEN secret 均已设置（`gh secret list`）

### 遗留事项

- 告警 issue「repo-health 周巡检告警 2026-09-12」：因 runlog 尚不足 5 份，属**预期行为**；CI 连续正常跑数日后 runlog 攒够即自动消失。若要清理，手动关闭该 issue（有去重逻辑，不会重复开）
- `state/data/calendar.json`、`state/data/status.json`：遗留本地件、无代码消费，已 gitignore，勿手动提交
