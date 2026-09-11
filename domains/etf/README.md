# 红利低波 · 情绪极值策略监控（每日 8:00 自动更新）

中证红利低波动全收益指数（H20269）v7.13 策略的实时监控产品：
**新手漏斗式仪表盘**：「① 今天做什么（人话结论+对应ETF+金额）→ ② 市场情绪温度计 → ③ 我的仓位（本金输入自动换算）→ ④ 策略概览 → ⑤ 回测证据/术语/买卖点（默认折叠）」，每天北京时间 8:00 由 GitHub Actions 自动更新并覆盖推送到 HSK 文件托管。

**v7.14 新增行业轮动策略（`#/sel-sector`，v1.0 → v1.1 审计整改）**：实现《ETF行业轮动量化策略方案》（2026-09）——21 行业 × 32 只 ETF 标的池、截面多因子得分（动量 50% / 波动率 20% / 拥挤度 20% / 反转 10%）、Top-4 月度轮动 + 15% 换仓门槛（z 分加法）+ 月中加速、四项风控（超额收益熔断[滚动窗口+绝对pp] / 单行业止损 / 市场波动率过滤 / 单月换手上限）、2018 至今回测；按《数据工程回测审计报告》（2026-09-11，18 项 P0/P1/P2）完成系统性整改：盘中半截 K 线清洗（以抓取时刻判定收盘）、过拟合体检（参数敏感性/Walk-Forward/随机对照 n=150）、基准补全（夏普/IR/同暴露折算）、成本模型拆分（滑点 5bp+佣金 max(万1,5元)）等；与红利低波同仓流水线（每日同 job 更新、输入存档 data/、幂等断言、HSK 发布自检）。

**v7.13 左侧目录导航**：页面左侧新增可点击的树形目录——一级「选ETF / ETF择时」；「ETF择时」下设 红利低波（已上线）/ 沪深300 / 中证500；「选ETF」下设 资产配置策略 / **行业轮动策略（已上线）** / 估值驱动策略 / ETF分类（三级：宽基ETF·中风险 / 行业主题ETF·高风险 / 跨境ETF·高风险）。除红利低波与行业轮动外的节点为得体空页（面包屑 + 建设中说明）。hash 路由（#/timing/hongli、#/sel-sector 等），移动端侧栏折叠为左下角抽屉按钮。

## 目录结构

```
repo/
├── engine.py / backtest/engine.py  # 统一回测引擎（红利低波；两处同文件，update.py 优先用 backtest/）
├── backtest/prep_data.py     # CSV 生成脚本（唯一合法来源，固化清洗规则：剔假日复制行/去重/剔NaN）
├── backtest/scan_sensitivity.py  # 过拟合体检
├── backtest/h20269_daily.csv / h30269_daily.csv  # 行情源（本地回测用，由 prep_data.py 生成）
├── backtest/sensitivity.json # 参数敏感性 + walk-forward 结果
├── trade_calendar.csv        # A股交易日历（2013-2026，akshare sina）
├── sector_engine.py          # 行业轮动统一引擎（v1.0：因子/回测/风控/快照，update 与回测共用单一实现）
├── sector_universe.py        # 行业轮动标的池（21 行业 × 32 ETF）与行情抓取（东财 + 中证官网）
├── sector_update.py          # 行业轮动每日更新：抓取→校验→引擎→生成 sector payload→注入 index.html→归档
├── sector_sensitivity.py     # 行业轮动过拟合体检（P0-4）：参数敏感性 + Walk-Forward + 随机选股对照 → data/sector-sensitivity.json
├── payload_util.py           # PAYLOAD 提取/注入（update.py 与 sector_update.py 共用，carry-forward 互不覆盖）
├── index.html                # 对外展示页（由模板+两套数据生成，推送此文件）
├── index_template.html       # 页面模板（占位符 __PAYLOAD__，注入红利低波 + 行业轮动两套数据）
├── update.py                 # 红利低波每日更新脚本：抓行情 → 校验 → 统一回测引擎 → 生成 index.html
├── sector_data.json          # 行业轮动独立数据段（幂等断言用，页面数据并入 index.html）
├── backtest_data.json        # 回测数据（日频全量净值/回撤抽样展示 + 历史买卖点，每日滚动重生成）
├── data/                     # 每日原始输入存档（H20269/H30269-YYYYMMDD.json、snapshot-YYYYMMDD.json、sector-raw-YYYYMMDD.json.gz、sector-snapshot-YYYYMMDD.json）
├── requirements.txt          # 依赖锁定（pandas / numpy，无 akshare）
├── tests/test_engine.py      # 红利低波引擎最小单元测试
├── tests/test_sector_engine.py  # 行业轮动引擎最小单元测试（26 项）
└── .github/workflows/daily.yml
```

回测引擎在本仓库 `backtest/engine.py`（唯一实现，update.py 与回测共用，杜绝双实现漂移）；
`backtest/scan_sensitivity.py` 产出过拟合体检（参数敏感性 + walk-forward）→ `backtest/sensitivity.json`。

## 本地运行

```bash
pip install -r requirements.txt
python3 -m unittest tests.test_engine -v           # 红利低波引擎单测（验证指标/状态机/T+1/净值复算/抄底闭环）
python3 -m unittest tests.test_sector_engine -v    # 行业轮动引擎单测（26 项：因子/选股规则/止损/熔断/波动率过滤/T+1/基准/挂单）
python3 sector_sensitivity.py                      # 行业轮动过拟合体检（P0-4）→ data/sector-sensitivity.json
python3 backtest/prep_data.py                      # 从接口重新生成行情 CSV（清洗规则固化）
python3 update.py                                  # 红利低波：在线抓行情 → 校验 → 生成 index.html + backtest_data.json
python3 sector_update.py                           # 行业轮动：在线抓取 → 校验 → 生成 index.html（含 sector 段）+ sector_data.json
python3 -m http.server 8000                        # 本地预览 http://localhost:8000/index.html（#/sel-sector 行业轮动）
```

## 统一回测引擎（backtest/engine.py，v7.13 口径）

- **信号与收益分离**：全部信号（布林/KDJ/RSI/63日动量/顶背离）在**价格指数 H30269** 上计算；收益用**全收益 H20269**（分红再投）。`px` 不再是死代码。
- **净值按全收益计价（v7.12 地基修正）**：持仓市值 = TR 归一份额 × 全收益指数（q=Σ买入额/买入日TR），策略端吃到分红再投，与买入持有（TR）同口径。此前用价格指数计价漏掉全部分红，策略收益系统性低估（v7.11 +168.2% → v7.12 +285.3%）。
- **T+1 撮合**：T 日收盘确认信号 → **T+1 收盘成交**（数据无开盘价，以 T+1 收盘近似，如实披露）；单边**滑点 5bp**。
- **杠杆真实成本**：125%/150% 仓位隐含 25%/50% 融资，按**年化 7% 按交易日计息**（每日计提）。
- **日频全量**：净值/回撤/夏普全部在日频全量数据上计算（每交易日一点），不抽稀；页面图表抽样仅为展示。
- **费用**：单边 max(金额×万1, 5元)；抄底胜率按含滑点成交价 FIFO 配对。
- 指标：布林 20 日均线±2σ（ddof=0）、周线 KDJ(9,3,3)（ISO 周重采样、J=3K−2D）、周线 RSI(14) Wilder、日线 RSI(14)（顶背离）、近 63 日动量（up63/dn63，超卖阈值 −14%，v7.10 由 15→14）；v7.11 新增双重门槛：价格 <250 日线（年线门）+ 估值剪刀差（股息率代理−10Y国债）3 年历史分位门（≥80% 正常 / 50-80% 半力禁150 / <50% 不执行）。股息率代理＝H20269/H30269 比值 12 个月滚动增长率（系统性偏高约 0.4pp，分位形态与官方一致，如 2024-09 高企；十年期国债为静态缓存 cn10y_daily.csv，2013-2026 日频，akshare bond_zh_us_rate 拉取）。

**回测结果（v7.12，2016-09-08 ~ 2026-09-08，初始 10 万）**：
策略 **+285.3% / 夏普 0.90 / 最大回撤 −29.0%**；买入持有 +134.3% / 0.62 / −27.5%。44 笔；抄底 20 档胜率 90.0%（平均 +5.6%）、加仓 2.0 次/年。
- v7.10 = 超卖跌幅阈值 15→14（walk-forward 三段一致增强）；v7.11 = 年线门 + 估值门；**v7.12 = 地基修正（净值按全收益再投计价，此前用价格指数计价漏掉全部分红，策略收益系统性低估）**。周线共振门/跌幅加速/REBUY45/动能T+3 回测否决见 HANDOFF 第 16-17 节。
- **顶背离三选二（DIV_2OF3）复查**：旧否决（回撤 −34.2%）是在 `tradingVol` 被列切片 bug 切掉（量价背离恒 False，实际退化为 RSI AND MACD 双 AND）的失真环境下测出的；v7.12 解封 vol 后重测，三段 walk-forward 与基线差异 <1pp、无增益，维持"仅 RSI 顶背离"（少 2 笔、更简单），开关保留可复现。
（v7.6 的 +328.5%/0.97/−27.6% 基于"同日收盘成交+零成本杠杆+TR 信号"，不可复现且系统性乐观，已废弃。）

## 行业轮动策略（sector_engine.py / sector_update.py，v1.0）

实现《ETF行业轮动量化策略方案》（2026-09）的**行业截面动量**策略，页面路由 `#/sel-sector`，与红利低波同仓、同 job 每日 8:00 更新。

- **标的池**：21 行业 × 30 只 ETF（`sector_universe.py`，流动性门槛日均成交额≥5000 万、规模≥2 亿、同行业取 1-2 只）。行业篮子＝成员 ETF 日收益等权合成；价格＝篮子累计净值 ×100；成交额求和、换手率取均值。
- **信号（截面多因子）**：动量 50%（20 日跳过近 5 日 + 60 日 + 120 日，年化 × 各自 R² 趋势质量后等权，截面 z 标准化）＋波动率 20%（20 日已实现波动率截面前 30% ×0.7）＋拥挤度 20%（60 日均换手 3 年分位 0.5 + 成交额占比 60 日变化 3 年分位 0.5，截面前 20% ×0.85）＋反转 10%（近 5 日跌幅截面前 10% +0.15）。
- **组合与调仓**：月度最后一个交易日决策、T+1 收盘成交，持有 Top-4 等权；换仓门槛 15%（新入选 ≥ 当前持仓最低分 ×1.15）、持仓排名前 60% 留仓观察；月中加速（持仓跌出前 50% 且候选 ≥ 持仓最高分 ×1.2，每月最多一次）；单月换手 ≤100%。
- **风控**：①超额收益熔断——策略近 20 日超额收益（vs 沪深300全收益）自高点回撤 >8% → 仓位 50% 暂停开仓，超额转正恢复（触发连续确认 5 日、熔断至少保持 10 个交易日防抖）；②单行业止损——自建仓成本回撤 >12% 无条件平仓；③市场波动率过滤——沪深300 60 日波动率 1 年分位 >80%→70% / >90%→50% 仓位上限（调仓日生效，避免逐日抖仓）；④换手上限 100%/月。
- **执行口径**：成交价＝执行日收盘 ×(1±单边 6bp)（双边 12bp，落在方案千 1~千 1.5）；回测 2018-01-01 至今，初始 10 万；基准＝沪深300 全收益 + 行业等权；年化 252 日、夏普无风险利率 0。
- **数据源（纯 HTTP，无 akshare）**：东财 `push2his` 日 K（前复权、含成交额/换手率）+ 中证官网 `index-perf`（H00300 全收益 / 000300 价格）；限流指数退避 + 并发 4。
- **v1 限制（页面如实披露）**：拥挤度暂缺「份额变化率」第三维（东财无稳定历史份额接口，按换手率 + 成交额占比两维合成）。
- **运行与测试**：
  ```bash
  ~/miniconda3/bin/python -m unittest tests.test_sector_engine -v   # 14 项单测
  ~/miniconda3/bin/python sector_update.py                          # 在线抓取→校验→生成 index.html + sector_data.json
  ```

## 每日更新逻辑（update.py）与可靠性（v7.12 加固）

- **数据时效**：中证当日收盘数据延迟发布（16:56 实测仍无当日数据），早 8 点跑必然取到 **T-1 日收盘**——页面 data_date 显示昨日收盘，供当日开盘前决策，属正常设计。
- **交易日历闸门（v7.12）**：页面不再无条件喊"今天可以加仓抄底"。读取仓库内交易日历（trade_calendar.csv，2013-2026，akshare sina），休市日把卡片标题改为"下个交易日（X）可加仓/清仓（今日休市）"，并显示蓝色休市横幅；交易日历缺失时降级为周末判断。
- **输入落库（v7.12）**：workflow `contents: write`，每次把抓到的原始响应（data/H20269-YYYYMMDD.json、H30269-YYYYMMDD.json）与当日快照（data/snapshot-YYYYMMDD.json）连同 index.html / backtest_data.json 一起 commit 回仓库——"某天页面说了什么、依据了什么数据"随时可还原复核。行业轮动同样归档（data/sector-raw-YYYYMMDD.json.gz 原始响应 gzip + data/sector-snapshot-YYYYMMDD.json 当日计算快照 + sector_data.json）。
- **固定左边界（v7.12）**：抓取窗口锚定 20130719（中证接口最早可回溯日），不再 `today−4800` 每天前移——每日数据严格是前一天的超集，任何一天的产出都可被另一天复核。
- **硬校验（v7.12）**：收盘价强转 float 拒 NaN/≤0、最新数据日必须是交易日且不得晚于最近交易日（防盘中占位值）、重复日期键拒绝、估值分位数缺失 >50% 直接红掉——**宁可 job 红、保留昨日页面，也不用残缺数据重算发布**。软警告（滞后 >3 天等）仍显示页面黄条。
- **版本锁定**：workflow 用 `requirements.txt`（pandas>=2.0,<2.3 / numpy>=1.24,<2.3）；engine 已清除 `fillna(method=)` 弃用调用。
- **发布自检**：workflow 末尾"Verify published page"比对线上与本地产物 generated_at（红利低波 backtest 段 + 行业轮动 sector 段分别校验）；新增发布输出校验（异常重试一次）与失败告警、`timeout-minutes`、幂等断言（同一输入跑两遍逐位一致，两套脚本各自断言）。
- **时间戳**：页面 generated_at 统一为北京时间（UTC+8）。
- **十年期国债缓存**：cn10y_daily.csv 为静态缓存（2013-2026），**建议每季度手动跑一次 `python3 refresh_cn10y.py` 刷新**（需 akshare，Actions 流水线不装）；缓存缺失时引擎补齐全部估值字段（div_proxy/y10/spread 全 NaN），不再 KeyError；估值门数据大面积缺失时流水线直接红掉（不再静默把超卖信号关成 0 后发绿页面）。
- **CSV 生成脚本入库（v7.12）**：`backtest/prep_data.py` 是 h20269/h30269 两份 CSV 的唯一合法来源——从接口重新生成并固化清洗规则（剔除非交易日假日复制行、统一日期格式、去重、剔除 NaN/非正值），解决"CSV 无主产物、20140101 幽灵行"问题。

## 每日更新逻辑（update.py）

1. **抓行情**：中证指数官网 `index-perf` 接口，抓 H20269（全收益）与 H30269（价格）自 **20130719** 起全部收盘价（固定锚定，覆盖回测起点、MA200、周线 KDJ 收敛、63 日动量与状态机重放余量；接口限流自动退避重试）。原始响应按日存档 data/。
2. **算指标 + 状态机重放 + 净值核算**：全部复用 `backtest/engine.py`（单一实现）。
3. **触发预告**：超卖缺口按**逐条件**展示（2-of-4 再中 N 个即触发；布林/63日跌幅缺口各自列出，63 日阈值按"明日窗口"修正），不再合成一个 max 数（原实现 2-of-4 用 max 会系统性高估缺口）。
4. **回测数据每日滚动重生成**：`backtest_data.json` 由引擎按最新行情重算（不冻结），指标用日频全量。
5. **生成自包含 index.html**：内嵌今日快照 + 回测数据，覆盖写入。

## GitHub Actions 定时发布

仓库需配置（GitHub 仓库 Settings → Secrets and variables → Actions）：

| 配置项 | 值 | 说明 |
|---|---|---|
| Secret `HSK_API_KEY` | `ph_key_…` | HSK 文件托管 API Key（用户持有） |
| Secret `HSK_RESOURCE_ID` | `1788920564682150822` | 覆盖推送同一资源（经 `secrets.HSK_RESOURCE_ID` 引用；该账号 Actions variables 端点不可用，故走 Secret） |

当前已发布资源：

- 公网地址：`https://945q5w.gicp.fun`
- 资源 ID：`1788920564682150822`
- 更新模式：`hsk-cli +host index.html --resource-id 1788920564682150822 --format json`（update 后 CLI 可能返回 `claimed:false/pending`，属正常现象，实测内容即时生效、URL 持续可访问，无需处理）

**启用定时任务**：把仓库根目录下全部文件推到 GitHub 仓库（保持目录结构），Actions 即按 cron（UTC 00:00 = 北京 08:00）自动运行；也可在 Actions 页手动 `workflow_dispatch` 立即触发。
注意：workflow 为 `contents: write`，每次更新把输入存档（data/）与产物（index.html / backtest_data.json / sector_data.json）回写仓库，仓库副本与线上发布版保持一致；

## 数据口径与免责声明

- 回测：2016-09-08 ~ 最新交易日（10 年，2427+ 交易日），初始 10 万，费用单边 max(万1, 5元)，滑点单边 5bp，融资年化 7% 按交易日计息，年化 252 交易日，夏普无风险利率 0。
- 超卖买点＝2-of-4 共振：J<1 / 跌破布林下轨 / dn63≤−20% / 周线 RSI<35 任意 2 个 → 加仓 125%（再触发至 150%）。
- A 态清仓＝超买三维极值：J>95 且 触及/突破上轨 且 up63≥20% → 离场 0%；离场后 60 自然日强制回补。
- 临时仓（125/150%）卖出＝**动能消失确认**，任一触发即了结回 100%：J 从>90跌破80 / RSI 从>70跌破65 / 10日顶背离（收盘创新高且日线 RSI 未新高）。
- 到期兜底：每档 25% 独立 60 自然日到期强制卖出。
- **过拟合体检**（`backtest/sensitivity.json`）：关键参数（J_LOW/J_HIGH/Y_DOWN/X_UP/RSI_OS/HOLD_DAYS/滑点/融资利率）±扰动，除 X_UP 外均位于"参数平原"（夏普波动 ≤0.05）；walk-forward 三区间夏普 0.45/0.56/0.75，无区间崩溃。完整 PBO / Deflated Sharpe 未实现，属剩余披露项。
- 行情源为中证官网官方日度数据，Actions 环境若被 WAF 拦截需调整抓取源；当前无多源兜底（剩余披露项）。
- 仅供策略验证与监控参考，不构成投资建议。125%/150% 超配隐含融资，实际执行需评估融资成本。
