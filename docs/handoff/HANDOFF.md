# handoff

域移交文档索引：

- 短线域（quant-lab）：HANDOFF.md 见原仓 /tmp/qh 检出（data 口径、除权修复、HSK 迁移史）
- ETF 域（red-dividend-strategy）：v7.x 版本史与 HSK 部署细节见原仓
- 个性化选股域（stock-factor-engine）：因子口径见原仓 scripts/compute_factors.py

> 本文件原名 `docs/handoff/README.md`（2026-09-21 改名）。仓根 `README.md` 第 8 行仍指向旧名
> —— 未改是为遵守「本次不动 README」的约定，属**已知悬空引用**，见文末「遗留事项」。

---

## 事件型数据存储：龙虎榜 + 涨停复盘（2026-09-22）

新增**事件型数据**（主键非 `(code, date)`，一票多因/按日聚合），独立于 K 线 asset/fq 体系，
统一放 `data/events/<table>/`，读写走 `common/store/events.py`，表定义在 `schema.EVENT_TABLES`。

### 需求

1. 龙虎榜：净买额 / 上榜原因 / 营业部席位 TOP5 / 机构动向，按**任意历史日期**查（近3年）
2. 涨停复盘：连板梯队 / N天M板 / 封单资金 / 涨停原因题材 / 晋级率
3. 全部日增量自动更新（GitHub Actions 定时）

### 表设计（schema.EVENT_TABLES，5 张）

| 表 | 主键 | 说明 |
|---|---|---|
| `lhb_detail` | (date, code, trade_id) | 龙虎榜主表：净买额/上榜原因/买卖总额/机构说明（trade_id 锚一票多因） |
| `lhb_seat` | (date, code, trade_id, side, seat_code) | 买卖席位明细：营业部名/买/卖/净额/rank（TOP5 从 rank 取） |
| `zt_pool` | (date, code) | 东财涨停池快照：封单资金/首末封板时间/炸板次数/连板数/N天M板/行业（**仅近~10交易日**） |
| `zt_daily` | (date, code) | 涨停自算：按板块阈值判涨停 + 连板数 + 近3/5/10日涨停次数（**全历史**） |
| `zt_ladder` | (date, lbc) | 连板梯队/晋级率：昨日 (k-1) 板 → 今日 k 板 晋升比例 |

### 存储布局与语义

- 封存分区 `events/<table>/year=YYYY/month=MM/batch=00.parquet`（zstd-19）+ 日增量 `_incr/YYYYMMDD/<table>.parquet`
- manifest：`data/manifest/events_<table>.json`
- **写入语义**：日增量同行数→跳过；行数不同→主键合并去重重写；**整月封存=整月重建**（先删旧分区
  再写，并**清该月所有 `_incr/YYYYMM*` 日分片**，防 load_events 重复时旧文件胜出——2026-09-21 华瓷
  股份连板读回错误就是这踩的坑，`_incr` 目录名是 8 位完整日期，须前缀 glob）
- **读取**：`load_events(name, dates=[...], start/end, codes, columns, root)`——`dates=` 即"按任意历史日期查"

### 数据源与口径限制（方案决策 2026-09-22）

- 龙虎榜：东财 datacenter `RPT_DAILYBILLBOARD_DETAILSNEW` + `RPT_BILLBOARD_DAILYDETAILSBUY/SELL`，
  实测支持任意历史交易日（2023-09-18 起验证）。"返回数据为空"（停市/当日盘后未公布）按合法空页处理
- 涨停池：`push2ex.eastmoney.com/getTopicZTPool`（ut=7eea3edcaed734bea9cbfc24409ed989），**仅近 ~10 交易日**
- 涨停自算口径：主板 10% / 创业科创 20% / 北交 30%，涨停价=round(prev_close*(1+thr),2)，close>=涨停价-0.001
- **已知限制**（用户已确认接受"自算+近端快照"主干方案）：
  1. ST 股 5% 涨停不特殊识别，按板块阈值判（universe 无 ST 标记）
  2. 涨停原因/题材**无历史公开接口**（东财/同花顺均无），历史用行业板块近似（近10日 zt_pool 有 hybk）
  3. 封单资金/封板时间仅近10日快照有；更早历史查 zt_daily 自算列
- 开盘啦源不可用（apph5.kaipanla.com NXDOMAIN；apph5.kaipan.la 海外 IP 本机不可达）→ 未采用

### 脚本

```bash
# 日增量（workflow data-events-incr.yml，北京 17:10，等 16:40 stock 增量先入库）
python -m tools.data_pipeline.lhb_incr   --data-root data --writer "data-events@run N"
python -m tools.data_pipeline.zt_pipeline --data-root data --writer "data-events@run N"
# 历史回补（近3年，按月封存；涨停快照仅近10日）
python -m tools.data_pipeline.lhb_incr   --data-root data --backfill 2023-09-01
python -m tools.data_pipeline.zt_pipeline --data-root data --backfill 2023-09-01
# 数据仓自检（已接入 check_event_table，events 分区一并校验）
python -m tools.data_pipeline.verify --data-root data
```

### 验证记录（2026-09-22 本地全链路）

- 龙虎榜 09-18：detail 58 行/seat 513 行，净买额 TOP5、席位 rank、机构专用席位聚合均正确
- 涨停自算 09-18：78 只**与东财快照完全一致**；09-21 连板分布 {1:81,2:14,3:5,4:2,5:1} 与快照 lbc 分布吻合
- zt_ladder 09-21：2板晋级率 21.2%、4板 100%（prev 2 只 → 2 只），华瓷股份 5 连板正确
- 全量回补完成：zt_daily 347 万行 + zt_ladder 3,352 行（2023-09~2026-09 共 37 个月封存）；
  lhb_detail + lhb_seat 各 37 个月封存（725 交易日，15 天因停市/盘后未公布合法跳过）
- 历史日期抽查读回（load_events dates=）：2023-11-10 / 2024-06-14 / 2025-08-15 / 2026-03-10
  净买额/上榜原因/席位/机构专用全正确；zt_pool 近 10 日窗口正常
- **verify 全绿**：daily 2570 分区 · 派生 14 · manifest 23 · events 158 分区
  （契约/不变量/日历/派生一致性/manifest 路径 全过）
- 事件存储往返/幂等/月份封存清理/缺主键保护：单测全过

### 关键经验（本次踩坑）

1. **字符串列绝不能 to_numeric**：events 归一化把 string[pyarrow] 列打成 NaN（name/reason/seat_name
   全毁），已按类型分支处理
2. **连板递推用"前一日连板数"而非"前一日是否涨停"**：`limit_count[i]=limit_count[i-1]+1`，否则
   3 板以上全被压成 2 板（华瓷股份 5 连板实测）
3. **增量首日连板全错**：zt_daily 增量依赖已入库历史尾部做连板追溯，**必须先 backfill 再增量**；
   workflow 顺序 stock→lhb→zt，且事件增量天然在历史回补之后才首次运行
4. **backfill 幂等必须整月重建**：封存月份时先 rmtree 旧分区再写，同时清该月 `_incr` 日分片
5. **manifest 与磁盘同步**：`write_event_month` 清理磁盘 `_incr` 分片后，**必须同步移除 manifest
   里该 `_incr` 条目**（否则 verify 报"分区路径不存在"）——已修：清理后读 manifest 过滤掉被删路径
6. **`load_events` 过滤后刷新日期 Series**：`dates=`/`start=`/`end=` 逐步过滤后要基于当前 df
   重算 `d`，否则布尔索引跨 index 触发 reindex 警告

### 遗留事项

- ✅ lhb 全量回补完成 + verify 全绿（2026-09-22）
- 事件型数据与 K 线不同：**无冻结语义**（同日二次抓取允许按主键合并修正），manifest 无 freeze 断言
- `zt_pool` 的历史只有近 10 日——每交易日增量自动滚动保留近 10 日窗口，过期日分片不自动删
  （体积 ~MB 级，暂不清理；如后续要裁剪可加 expire 逻辑）
- README 未提及 events 目录（遵守"不动 README"约定），如需文档化待后续

---

**故障现象**：2026-09-14 起 `data-stock-incr` 定时任务连续失败，个股数据仓停在 09-11
（index/etf 正常）。三根因 + 数据缺口全部定位、修复、回补并验证。

### 根因（3 个独立缺陷）

| # | 缺陷 | 位置 | 修复 |
|---|---|---|---|
| 1 | `detect_dividends` 把 `prev_raw` **元组直接除** → `TypeError`，stock 增量 09-14 起崩溃 | `stock_incr.py` | 取元组第二元素（commit f2a7ff4 + 7674a11） |
| 2 | `csindex.backfill_daily` **整月覆盖**抹掉同月其他 code | `csindex.py` | 改为逐月合并/补缺 |
| 3 | `csindex.fetch_csi_rows` **空返回不重试** → ETF 抓取偶发缺日 | `csindex.py` | 空返回重试 |

另：GitHub push 偶发网络故障（HTTP2 framing / 443 超时）→ 三个 data-*.yml 均加
**push 失败重试 3 次 + `git pull --rebase --autostash`**（commit 3540fbb）。

### 数据恢复（写回补脚本全量补齐）

- **index / etf**：内容等价分区还原 HEAD + 真实变更提交（sh000852 2026-09-21 保留），
  verify 全绿，远程 data-index / data-etf-incr 09-21 排程均 success。
- **stock 缺日 2026-09-14..21**：全 A（universe 5215）新浪日K回补
  （`CN_MarketData.getKLineData`，scale=240；**腾讯 ifzq 已被 IP 限流禁用**；
  新浪 volume 单位为**股**，amount 兜底 = close×vol）。抓取 5209 有效帧/fail=6，
  断点缓存 `/tmp/stock_gap_cache.parquet` + 连续失败 10 只 sleep 90s 节流。
- **封存**：`seal_partition` 只收集该月 `_incr` 整月重建 → **直接调用会覆盖丢既有封存
  （09-01..11）**。用合并脚本（读 封存+_incr → 去重 → 重建 month=09 → 删 _incr →
  更新 manifest）封存：raw 65121 行/5210 codes、hfq 65911 行/5211 codes、
  09-01..21，sealed=true。commit `42656aa`。
- **手动 dispatch 验证**（run 35676511273）：data-stock-incr **success**（目标日 09-21
  增量已存在 → 合法幂等跳过，链路完整跑通）。远程 CI 自动提交 runlog + 09-22 数据
  `db33858`。本地 verify 全绿。

### 关键经验

1. **封存前先合并**：`seal_partition` 语义是「该月 `_incr` 分片 → 整月分区」，绝不适用于
   「分区已有数据 + 补缺 _incr」场景，必须先读全量合并再重建。
2. **内容哈希别用 `.tobytes()`**：Arrow string 列会序列化指针地址 → 等价内容每次哈希
   不同（index 清理时踩坑），改用 dtype 规范化 + `DataFrame.equals()` 值级比较。
3. **数据源限流**：腾讯 ifzq 全量批量拉会被 IP 封禁（空响应），新浪日K是可靠备源；
   回补脚本需断点续传 + 节流。

### 遗留事项

- stock 回补 6 只 fail（约 12 只缺 09-21 数据，多为停牌/新上市），下一交易日 schedule
  自然覆盖，无需人工。
- 本次新浪回补 amount 为兜底口径（close×vol），与腾讯源一致；如后续发现与官方成交额
  有出入，可对该 6 日局部重拉。
- **今日（09-22）16:40 北京 schedule 为修复后首次真实定时运行**（将抓取 09-22 增量并
  处理 000155 除权走 ifzq 修复）。历史实测 GitHub schedule 触发有 5~7h 延迟（09-21 那
  次 15:18Z=北京 23:18 才创建），故预计北京晚间出结果。后台监控脚本
  `/tmp/stock_schedule_watch.py` 已挂起轮询（日志 `/tmp/stock_schedule_watch.log`），
  发现新 schedule run 后自动跟踪至 completed 并记录成败。收尾动作：检查该日志，若
  success 且数据仓出现 `_incr/20260922` → 定时拉取恢复正常，可标记目标完成。

---

## 三域外观统一（2026-09-21，commit 见 PR）

把三域从「三套独立长出来的外观」统一为同一套视觉规范：配色令牌、容器宽度、卡片处理、
导航/标签组件规范、**涨跌色方向**全部对齐；并顺手修掉一个线上缺陷。

### 动机

三域前端是三套独立演化的单文件 HTML，同一页面内并排呈现出三套风格（例：ETF 域是冷蓝底
`#F6FAFE`，选股域是浅蓝底 `#F4F6FB`，短线域是暖灰 `#F6F6F4`；侧栏宽度 170 / auto / 240
各不相同；导航选中态一个黑底白字、两个浅蓝底蓝字）。产品上是一个页面，看起来却是三个站。

### 改动文件

| 文件 | 说明 |
|---|---|
| `web/shell/scope.py` | 唯一真相源：`PALETTE`（配色）、`COMPONENTS`（语义组件层）、`NAV_MEDIA`（响应式）、`UNIFY_MAP`（模板里写死的色值）；`unify_colors()` / `theme_block()` / `component_css()` / `nav_media_css()` |
| `web/build.py` | 壳层 `SHELL_CSS` 令牌化（`$TOKEN`）；顶栏由深色 `#1F2430` 改浅底 + 下描边；`.qh-tab` 与域内标签共用 CHIP 规范 |
| `tests/web/test_scope.py` | 新增 24 条断言（令牌一致性、组件层展开、文档顺序、响应式只发本域、前缀替换回归…） |
| `conftest.py` | 加 `sys.dont_write_bytecode = True`（理由见下「防假绿」） |
| `docs/handoff/README.md` → `docs/handoff/HANDOFF.md` | 本文件改名 + 新增本节 |

### 设计要点（改这几处前先读）

1. **两层唯一真相源**：`PALETTE` 管颜色，`COMPONENTS` 管组件形状与交互态。只做前者统一不了——
   三域导航的类名几乎不重叠（全站只共有 `brand`/`card` 两个 class），无法靠「同名类覆盖」，
   只能做**语义映射**（源头写一次 → 逐域展开成各自的选择器）。
2. **★ 顺序硬约束**：`component_css` → `theme_block` → `nav_media_css`，且**必须都排在域样式之后**。
   它们与模板被改写的 `#app-{d}{}` 块**同优先级**，胜负只由文档顺序决定 —— 放前面会被模板原值
   静默覆盖（表现=颜色还是各域老样子，不报错）。`nav_media_css` **必须排最后**，否则顶不掉
   移动端抽屉样式。测试里有对应断言挡着。
3. **涨跌语义统一为中国惯例（红涨绿跌）** —— 见「风险」。
4. **模板自带的 `:root{}` 不去改**：作用域化后 `_promote_globals()` 把 `html/body/*/:root` 改写为
   `#app-{d}`，再由令牌块靠顺序覆盖。

### 验收证据

全部为**可复现命令 + 独立指标**，不接受「看起来对了」：

- **产物级 7 项**（`verify.py`）：旧硬编码色 **198 处 → 0 处**（36 种旧色值逐个清零）；
  三域令牌块各 1 份且 **23 个令牌逐字相同**；组件层三域都在、侧栏宽 **192px**；
  文档顺序 组件层 < 令牌块 < 响应式块；`/*__DATA_DATE__*/` **1 → 0**；`$TOKEN` 残留 0。
- **基线保真**：改前产物 md5 `64b04d15f66d106bf6d9064beea060ec` / 2,612,652 字节，与线上 Pages
  **逐字节一致** → 对比基线可信（否则「改动前后」毫无意义）。
- **DOM 级实测**（注入探针 + `--dump-dom` 读 `computed style`）：三域 `--up/--down/--bg` 归一；
  侧栏 192px sticky；导航选中三域一律黑底白字；顶栏浅底 + 1px 描边；**选股 `+17.6%` 由绿
  `rgb(22,163,74)` 变红 `rgb(213,66,62)`、`-22.4%` 由红变绿**。
- **单测 62 passed**；**反向验证 4/4**（挪顺序→3 域红 / 响应式串域→2 域红 / 192→170px→3 域红 /
  顶栏改回深色→1 failed），证明测试不是恒绿。
- CI `build-page` 五项冒烟（三域作用域根 / 无 CDN / 导航齐全 / **无死规则** / echarts 内联）通过。

### ★ 风险（两条，务必知悉）

1. **选股域涨跌语义反转**。该域原为美股惯例（绿涨红跌），本次翻向为中国惯例（红涨绿跌），
   且 `--up/--down` 三域统一为 `#D5423E` 红 / `#1D9E75` 绿。这是**有意的语义变更**，不是
   纯样式调整：任何依赖该域红绿含义的下游（截图、说明文案、用户肌肉记忆）都要一起改。
2. **硬编码色是按字面量替换的**。`unify_colors()` 走 `UNIFY_MAP` 逐色值 `str.replace`，
   所以：**将来该域新写入一个不在表里的写死色，就不会被统一**（例如选股涨跌翻向会「只翻一半」：
   CSS 走 `var(--up)` 会翻，JS 里新写死 `#16A34A` 不会翻）。**新代码一律走 `var(--up)/var(--down)`，
   不要写死十六进制色**。同理，`UNIFY_MAP` 是「枚举当前已知色值」，不是规则。

### ⚠️ 防假绿（本次踩到并封堵）

- **陈旧 `.pyc` 能制造静默假绿**：CPython 的失效判据是 `(mtime 秒, size)`，所以「等长改动
  （如交换两个调用顺序）+ mtime 落同一秒」的源码修改会继续跑**旧缓存**。受控实证：不清缓存
  → 62 passed（**错误顺序照样全绿**），清缓存 → 3 failed。CI 是新检出所以碰不到，**本地才会骗人**
  → `conftest.py` 加 `sys.dont_write_bytecode = True`。
- **`pytest.skip` 指向不存在的路径 = 真实测试从未跑过**：原用例在模板缺失时 skip，而那个路径
  本就不存在 → 三域真实模板测试一条都没跑、却一直显示全绿。已改为 `pytest.fail`。
- **本机绿 ≠ CI 绿：解释器版本差异**。CI 跑 **Python 3.11**，本机是 **3.14**。一个 f-string
  `f"{re.findall(r'\$\w+', css)}"`（表达式内含反斜杠）在 3.14 完全合法（PEP 701 于 3.12 放开），
  在 3.11 是 **SyntaxError** —— 测试**连收集都没过**，`build-page` 因 `needs: test` 一并被卡。
  **⚠️ `ast.parse(src, feature_version=(3,11))` 抓不到这个坑**（实测它照样返回通过）→
  唯一可靠的 3.11 护栏是**真的用 3.11 跑一遍**，别再用 `feature_version` 自我安慰。

### 遗留事项

- 仓根 `README.md` 第 8 行仍写 `docs/handoff/README.md`（改名后悬空）。本次约定不动 README，
  待后续一并修正。
- `domains/{etf,selected}` 域内现有硬编码色是**按当前字面量**清零的，新增写死色需同步扩
  `UNIFY_MAP`（见风险 2）。

---

## 阶段C/D/E 交接（2026-09-13，阶段A/B 之后）

依据《quant-hub-合并方案》§2.7/§7.1-7.3/§13.2 + grill-me Q1/Q2/Q5 决策实施。本阶段把「数据仓 → 短线策略链」打通：回测中间层、数据仓物化、shadow 影子链逐位比对与晋级台账。

### 阶段C：指数保留期资产分工固化（Q2，commit 87ef344）

- `schema.RETENTION`：`asset="etf"`（普通指数 H20269/H30269/H00300/000300）= **2430 交易日**；`asset="index"`（仅基准 sh000001/sz399001）= **None 全史冻结**，绝不可删
- `schema.check_retention` 防御：基准指数严禁传非 None（expire 按整月目录删，误传会删光 1990 起大盘全史）；expire 读契约单一来源
- `tools/data_pipeline/verify.py::check_derived` 已同时覆盖 `index_*` 与 `etf_*` 派生 manifest（阶段A/B 遗留事项已闭环）

### 阶段D：回测中间层（Q3，commit 867146b）

- 摘要入代码仓：`run_shortterm` 写 `state/shortterm/backtest_summary.json`，**KPI 口径与 `tools/shadow_diff.extract_kpis` 逐位一致**（momentum/blackbox × on/off 各 6 项，冻结防漂移）
- 明细入数据仓（不进代码仓）：`data/state/shortterm/backtest/<data_date>/`，只留「给定 data/+sha 可逐位重算」的判决性输出（equity/trades/holdings/signals/market_regime/signals_bb），排除 flags_long.parquet（65M）与 lgbm_scores.parquet（9.9M）

### 阶段E：数据仓 → 短线链（strategy-pm 影子链）

- **E-1** meta 种子化（commit 1877043）：数据仓 meta 与 universe/st_history 1:1 搬运
- **E-2a** 物化器 `tools/data_pipeline/materialize_qlab.py`：数据仓 → quant-lab 布局。幂等约定：`_clear_kline` **先清顶层分片 + incremental + fixup**（防 reader.load 已并入的 fixup 被残留件二次叠加）→ `reader.load` 全量重建 `data/kline/{fq}_{mkt}_{nn}.parquet` 平铺分片 + `data/meta/{stock_basic,st_history,index_daily,bench_daily,csi1000_daily}`；csi1000 缺失降级跳过不阻塞
- **E-2b** `run_shortterm.py --data-root`：跑计算链前先物化（shadow 侧数据源切换点）；不传则用 quant-lab 检出自带 data/（legacy 直跑模式，与旧 CI 完全一致）
- **E-2c** `strategy-pm.yml` 新增 `shortterm-shadow` job（`workflow_dispatch run_shadow=true` 才跑，默认关）：`cp -R` **独立检出**（绝不覆盖 legacy 侧 src/quant-lab 的 data/）→ `--data-root` 物化自产 `state/payload_shadow` → `shadow_diff --tol 1e-9` → 晋级台账 `state/shadow/ledger.json`（**连续 3 交易日 PROMOTE-READY 才晋级**，§7.2 Phase-4）。shadow 失败/BLOCKED 绝不阻塞主线
- **Q1 决策**：stock 保留期 **730 → 1250 交易日（5 年）**（schema.RETENTION + retention.py + data-retention.yml 同步）＋ 回补 **2023-01..08**（`tools/data_pipeline/stock_backfill.py`，已执行：hfq year=2023 month=01 起有数据）。动机：原 730 会被日级增量 expire 删掉回测起点 2023-09-01 所需 hfq 预热窗口（ret120 需 2023-01 起），shadow_diff 永远无法逐位晋级

### ★ 本场关键发现：legacy raw fixup 口径缺陷（shadow_diff BLOCKED 根因）

- quant-lab 检出自带的 `data/kline/fixup/raw_0_*.parquet`（28 只）是 **手单位 volume + 无 amount 列** 的旧格式；数据仓 raw 是 **股单位 + 真实 amount 全量**
- 后果：`build_indicators` 的 `amt = amount.fillna(close*vol*100)` 大量走兜底近似，amt20 掺入手/股 100 倍量纲错乱 → 入口绝对阈值 `m5 = amt20 >= 3e7` 翻转 → 动量信号系统性分歧（legacy 87,245 vs shadow 81,961，trades 473 vs 459）
- 修复：quant-lab 数据重建为数据仓口径（本地 src/quant-lab 与 /tmp 双检出均已物化重建，fixup 清空）。修复后双侧 payload **shadow_diff = PROMOTE-READY**（data_date/kpi/sha 逐位一致，台账 1/3）

### quant-lab 远端数据重建（遗留闭环，2026-09-13，commit 1519177549）

- **内容**：`andy-develop/quant-lab` 远端 `data/kline/fixup/` 56 个文件（28 只 × raw_0_/hfq_0_）由旧格式重建为**数据仓口径**——raw 8 列（股单位 volume + 真实 amount），hfq 6 列复权同步至 09-11；commit `1519177549`（"fix(data): 重建 28 只除权修复件为数据仓口径…"）推送 main（11220232 → 1519177549）
- **前置补数**：数据仓先补 09-07..11 五日增量（raw/hfq `_incr/` 平铺），`reader.load` 全史至 09-11
- **推送方式**：本地直连 GitHub 克隆/推送极慢（代理仅 curl 快），改用 `gh api` git blobs/trees/commits/refs 端点逐文件 base64 推送；blob tree 幂等，可复跑
- **验证**：修复后本地严格 CI 模拟（legacy 直跑 vs 数据仓物化 shadow）→ `shadow_diff` 两域（momentum/blackbox）**全 PROMOTE-READY**（max_kpi_delta=0.0，指纹一致）；远端 fixup 内容抽查（raw_0_000672：8 列、09-07 vol=182821/amount=274550000；hfq_0_000672：6 列、895 行至 09-11）

### ★ 增量单位缺陷修复（commit 1df2e6d7814a，2026-09-13，1519177549 假阳性闭环）

- **缺陷**：commit 1519177549 重建 fixup 时**直接复用远端 `data/kline/incremental/raw_20260907..11`**——该批增量是**手单位 volume**（000672 09-07 = 182,821 手）；而数据仓 stock_incr 规范化为**股单位** `round(amount/close)` = 18,266,800。上一轮验证看到的「vol=182821」实为**两侧同用一份手单位增量**的同源假阳性——shadow_diff 逐位一致 ≠ 与数据仓一致
- **修复**：按 `tools/data_pipeline/stock_migrate.py::to_contract_raw` 权威口径规范化（`volume_股 = round(amount/close)`、amount 取源值 >0）；fixup `raw_0_*` 增量段与 incremental raw 同步修正（5 文件 CHANGED），incremental/fixup 的 hfq 不动（blob sha 与远端原样一致）。commit `1df2e6d7814a` 推送 main（1519177549 → 1df2e6d7814a，fixup 56 + incremental 10，`gh api` git blobs/trees 幂等）
- **重验（真一致）**：T3 端到端模拟——legacy 直跑（修正后 fixup+incremental）vs shadow 物化（--data-root 数据仓）→ K线 4,337,158 行逐位一致、信号 82,168、交易 474/875（动量）与 498/924（黑盒）；`shadow_diff` 两域全 **PROMOTE-READY**（max_kpi_delta=0.0，payload_sha 相同）；000672 09-07 两侧 vol 均为 **18,266,800**（真一致，非假阳性）

### ★ base 沪市 600 前缀手单位损坏修复（commit dd2136008c22，2026-09-13，1df2e6d7814a 验证假阳性闭环 2 号）

- **缺陷**：quant-lab base 沪市 `raw_b0_*`（600 前缀 2,060 只，2023-09-01~2026-09-04 共 729 行/只）是 **baostock 手单位旧格式**：volume=手、**amount=0 全线**（600519 2024-03-01 vol=26,868/amount=0）；深市 raw_b1_* 正常（000001 vol=182,810,290/amount=1,917,689,306）。本地数据仓 2024-03 的 600 前缀 10,473/97,543 行（10.7%）与 base **逐行相同**——本地数据仓分区（迁移时）被重建成了与 legacy 同源的损坏旧格式 → 此前「真一致」为**同源假阳性**；远端数据仓为正确股单位（600519 vol=2,686,800/amount=4,527,419,208）
- **修复**：以远端数据仓为权威，本地全量下载远端 stock 分区（raw 891 + hfq 925 batch，`gh api git/blobs` base64 并行，github.com 443 被阻断走 api）→ `materialize_qlab --data-root /tmp/qhdata/data` 重建 base：`data/kline/` 22 片（raw/hfq 各 11）+ `data/meta/` 5 件，**删除旧顶层分片 + qfq（已弃用）+ raw_000 + fixup/（56）+ incremental/（10）**。commit `dd2136008c22` 推送 main（1df2e6d7814a → dd2136008c22）
- **重建后**：raw 3,445,610 行/4,966 只、hfq 3,582,955 行/5,145 只；600519 2024-03-01 vol=2,686,800/amount=4,527,419,208（amount==0 归零）；index/bench/csi1000 从 2023-01-03 起（物化器读本地 index daily 封存，远端仓缺 daily 封存已用本地件补齐验证）
- **数据范围收敛**：远端 stock raw 仅 2023-09 起（legacy 沪市同起点）；深市旧 base 2023-01 起 → 重建后两侧同 2023-09 起，engine 回测窗口由 flags_long 起点决定、两侧一致（2023-01~08 深市不补远端，不影响 shadow 一致性验证）
- **F8 真一致重验**：本地严格 CI 模拟——legacy 直跑（重建 base）vs shadow 物化（--data-root /tmp/qhdata/data）→ K线 3,445,610/3,582,955 行、信号 78,297、交易 418/738 笔、KPI 完全一致 → `shadow_diff` **PROMOTE-READY**（max_kpi_delta=0.0，payload_sha 相同 `69a844d5cd7d0a08`，台账 1/3）

### ★ shadow 真实 CI 首次跑通（2026-09-13：fixup 发现 + index daily 补充 + checkout ref 根因修复）

#### 1. fixup 发现（dd2136008c22 假阳性闭环 3 号，commit 11a9bf8dd6ab 重推 base）

- 远端数据仓存在 `data/market/stock/fixup/hfq_*.parquet` **28 只 hfq 覆盖件**，首次物化漏下载 → hfq 3,582,955 行 vs CI 预期 3,587,037（**差 4,082 行**）
- 补下载后重物化对齐（raw 3,445,610 / hfq 3,587,037 / 信号 78,297 / 交易 431/750，payload_sha=`710fc7823f063ee7`）→ 重推 quant-lab base commit `11a9bf8dd6ab`（含 fixup）

#### 2. index daily 封存补充（F11，远端数据仓 main → 5a6324940）

- 物化器 `materialize_index_daily` 依赖 `market/index/*/{code}/daily/**/*.parquet` **封存布局**；远端只有 monthly/weekly + raw 平铺 → CI 首次 shadow 物化抛 `FileNotFoundError: no index daily data for sh000001`
- 从本地上传 135 个 daily 封存（broad/sh000001 + tencent/sh000300/sh000852，`/tmp/data-index-push.py`，4 次重试退避）→ 远端补齐

#### 3. CI 三次运行链（workflow_dispatch run_shadow=true）

| run | 主线 | shadow | shadow_diff |
|---|---|---|---|
| 34761446273（14:01） | success | **失败**（index daily 缺失 → 上表 F11 修复） | — |
| 34762610257（14:25） | success | success | **BLOCKED**（legacy=`69a844d5cd7d0a08` vs shadow=`710fc7823f063ee7`，KPI 全等） |
| 34792840949（ref: main 修复后） | success | success | **PROMOTE-READY**（sha 两侧均 `710fc7823f063ee7`，max_kpi_delta=0.0） |

#### 4. ★ BLOCKED 根因：shadow job checkout 默认 ref 陷阱（commit 8673ef9）

- `shortterm-shadow` 第一步 `actions/checkout@v4` **无 ref** → 默认检出 **GITHUB_SHA（dispatch 时的提交）**，`state/payload` 还是主线 job 推送前的旧版本（run 3 旧 base dd2136008c22 的 payload，legacy=`69a844d5cd7d0a08`）；shadow 侧是新 base（`710fc7823f063ee7`）→ KPI 全等但归一化指纹不同 → **误报 BLOCKED**
- 修复：该步显式 `ref: main`（步骤执行时解析 ref，`needs: shortterm` 保证主线已 push 完成），workflow 注释同步更正
- 验证：run 34792840949 → quant-lab 域 `data_date/kpi/sha 逐位一致` → **PROMOTE-READY**；台账 `last_verdict=PROMOTE-READY`（09-11 已记过 BLOCKED，同交易日幂等不重复计数 → 下个新交易日 PROMOTE-READY 才计 1/3）



### 验证记录

- shadow_diff：`quant-lab: data_date_equal=true / kpi_equal=true / sha_equal=true → PROMOTE-READY`（threshold=3；增量单位修复后重验、base 重建后 F8 重验、真实 CI run 34792840949 均全 PROMOTE-READY；run 34762610257 BLOCKED 为 checkout ref 陷阱误报，commit 8673ef9 修复后根除）
- pytest 分域全绿：合并层 **293 passed / 3 skipped** · shortterm 43 / 9 skip · etf 64 · selected 11（README 命令逐条执行）
- 数据仓：hfq 有效截止 09-11（fixup 全史）、raw 止 09-04；回补后 hfq 2023-01-03 起

### 遗留事项

- ~~`andy-develop/quant-lab` 远端 data/kline 需重建为数据仓口径~~ → **已闭环（commit 1519177549 + 1df2e6d7814a + dd2136008c22 + 11a9bf8dd6ab，见上）**：远端 legacy 数据源现为数据仓口径（含增量单位缺陷修复 + base 沪市 600 前缀手单位修复 + fixup 覆盖件并入），CI 主线直跑与 shadow 物化不再因 fixup/增量/600 前缀分歧
- ~~shadow 链首次真实 CI 跑~~ → **已闭环（2026-09-13）**：三次 dispatch（34761446273 shadow 失败 index daily → 34762610257 双 success 但 BLOCKED → 34792840949 **PROMOTE-READY**）。完整链见上「shadow 真实 CI 首次跑通」
- 远端数据仓 **2023-01~08 深市 stock 不补**（重建后两侧同 2023-09 起，engine 回测窗口一致；不影响 shadow 一致性，可选）
- `_build_summary` 兼容 envelope dict 与 DomainPayload 对象两种形态（tests/tools/test_run_shortterm_summary 口径冻结）

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
