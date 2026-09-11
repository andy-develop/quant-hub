# HANDOFF · 红利低波情绪极值策略（v7.6 定稿）项目交接

> 最后更新：2026-09-09 · 状态：v7.6 定稿，线上监控产品已上线
> 公网地址：https://945q5w.gicp.fun（每日 8:00 自动更新）

---

## 1. 项目是什么

用户自建的量化策略工程：对**中证红利低波动全收益指数（H20269）**做 10 年回测（2016-09-08 ~ 2026-09-07），策略核心 = **常态满仓 100%，只在情绪极值时超配抄底（125%/150%）**，并用动能消失/到期纪律了结超配仓。配套一个每日 8:00 由 GitHub Actions 自动更新、经 hsk-cli 覆盖推送公网的 HTML 监控产品。

一句话策略：**平时满仓吃红利，恐慌时融资抄底，动能耗尽或到期后把杠杆仓卖掉**。

## 2. 目录结构

```
/home/user/Doubao/chats/38440664656484354/
├── HANDOFF.md                 # 本文档
├── backtest/                  # 回测工程
│   ├── backtest_v7.py         # v7.6 定稿回测脚本（唯一真相源）
│   ├── backtest_v7_v72_backup.py  # v7.2 备份（历史参照）
│   ├── verify_v7.py           # 独立验证脚本（指标重算/费用/分类/关键交易）
│   ├── metrics.json           # 回测指标
│   ├── equity_curve.csv       # 净值曲线（2427 行）
│   ├── trades.csv             # 43 笔交易明细
│   ├── h20269_daily.csv       # 全收益指数日线（回测标的）
│   ├── h30269_daily.csv       # 价格指数日线
│   ├── cn10y_daily.csv        # 十年国债收益率（v3 估值差实验用）
│   └── prep_report.py / report_*.html  # 早期报告管线（仍 v7.2，未同步）
└── product/                   # 线上监控产品
    ├── update.py              # 每日更新：抓行情→算指标→状态机重放→生成 index.html
    ├── index_template.html    # 页面模板（占位符 __PAYLOAD__）
    ├── backtest_data.json     # 10 年回测静态数据（952 采样点 + 43 笔）
    ├── index.html             # 生成的成品（推送到线上）
    ├── README.md              # 部署与运行说明（已同步 v7.6）
    └── .github/workflows/daily.yml  # GH Actions：UTC 0:00 = 京 8:00 自动更新推送
```

## 3. 回测口径（v7.6 定稿，backtest_v7.py 为准）

| 项目 | 口径 |
|---|---|
| 标的 | 中证红利低波动**全收益**指数 H20269（收盘价） |
| 区间 | 2016-09-08 ~ 2026-09-07，2427 交易日 |
| 初始资金 | 10 万 |
| 费用 | 单边 max(成交额×万1, 5 元) |
| 成交 | 信号当日收盘确认、当日收盘价成交 |
| 年化 | 252 交易日 |
| 夏普 | 无风险利率 0 |
| RSI | Wilder 平滑 |
| 周线 KDJ(9,3,3) | ISO 周重采样，K/D 初值 50，J=3K−2D，周末值回填 |
| 布林带 | 20 日均线 ± 2×标准差（ddof=0） |
| 63日动量 | lo63/hi63 用 shift(1) 前一日窗口，避免当日自洽 |

## 4. v7.6 信号定义（产品与回测完全一致）

**超卖（买点）＝ 2-of-4 共振**，任意 2 个满足即触发：
1. 周线 J < 1
2. 收盘 ≤ 布林下轨
3. 近 63 日跌幅 dn63 ≤ −20%
4. 周线 RSI(14) < 35

触发效果：A 态 100%→125%；C 态（已 125%）再触发→150%；B 态（离场）回补并加仓至 125%。

**超买（A 态清仓）＝ 三维极值**，全部满足才触发：
- 周线 J > 95 且 收盘 ≥ 布林上轨 且 近 63 日涨幅 up63 ≥ 20% → 清仓离场至 0%

**动能消失（临时仓 125/150% 卖点）＝ 任一触发即了结回 100%**：
1. `j_cross`：周线 J 从 >90 跌破 80（rolling(10).max() > 90 且 J ≤ 80）
2. `rsi_cross`：周线 RSI 从 >70 跌破 65（rolling(10).max() > 70 且 RSI ≤ 65）
3. `diverg`（10 日顶背离）：收盘 > 前 10 日最高收盘（shift(1)）且 日线 RSI < 前 10 日最高 RSI（shift(1)）

**到期兜底**：每档临时加仓 25% 独立计时 **60 个自然日**，到期未了结则强制卖出一档；B 态离场后 60 自然日内强制回补至 100%。

**参数表**：`HOLD_DAYS=60`、`J_LOW=1.0`、`J_HIGH=95.0`、`J_CROSS_FROM=90.0`、`J_CROSS_TO=80.0`、`RSI_OS=35.0`、`RSI_CROSS_FROM=70.0`、`RSI_CROSS_TO=65.0`、`X_UP=20.0`、`Y_DOWN=20.0`、`MAX_POS=1.50`、`START="2016-09-08"`、`LOOKBACK_DAYS=3800`。

## 5. 回测结果（v7.6）

| 指标 | 策略 | 买入持有 |
|---|---|---|
| 总收益 | **+328.5%**（终值 428,480.75） | +132.1%（终值 232,045.69） |
| 年化 | 16.3% | — |
| 夏普 | 0.97 | 0.61 |
| 最大回撤 | −27.6%（谷底 2018-10-18） | −27.5% |
| 交易 | 43 笔（买 24 / 卖 19） | — |
| 费用 | 481.47 元 | — |

**仓位分布**：空仓 7.6%、100% 占 73.6%、125% 占 6.3%、150% 占 12.5%。

**卖出构成（19 笔）**：动能消失了结 9 笔（顶背离 8 + J跌破80 1；RSI跌破65 0 笔，被抢先）+ 超买三维清仓 5 笔 + 到期卖一档 5 笔。

**抄底胜率（20 档超卖抄底，FIFO 配对）**：85.0%（17 盈/3 亏），平均单档 +5.52%；3 笔亏损全部来自到期强制卖出（2018-07-06/09 与 2026-05-18 抄底）。

## 6. 策略演进史（用户每一轮都推翻重测）

| 版本 | 核心改动 | 总收益 | 夏普 | 回撤 | 交易 | 备注 |
|---|---|---|---|---|---|---|
| v1 | 趋势过滤+布林四象限+RSI 确认 | — | — | — | — | 用户弃用 |
| v2 | 熊市黄金坑/牛市泡沫矩阵 | — | — | — | — | 用户弃用 |
| v3 | 估值差（股息率−国债）择时 | — | — | — | — | 用户弃用 |
| v4 | 五档仓位 0/25/50/75/100 + 周线 J | — | — | — | — | 用户弃用 |
| v5 | 卖出改周线 MACD | — | — | — | — | 用户弃用 |
| v6 | 永远满仓 + 极端抄底 125%（3 个月了结）+ 涨 20% 卖出必回补 | — | — | — | — | 用户弃用 |
| v7 | 情绪极值共振（J×布林×63日动量）+ 阶梯 125→150% | +322.8% | 0.99 | −28.1% | 31 | |
| v7.1 | 卖出放宽 J≥50 即了结 + 63 交易日改 60 自然日 + 蓝白配色 | +275.0% | 0.97 | −26.9% | 30 | **曾推送线上** |
| v7.2 | 主导条件对应正常化任一先到 + 周线 RSI(14) 35-70 | +275.0% | 0.97 | −26.9% | 30 | **曾推送线上** |
| v7.3 | 2-of-4 共振（Y=10） | +236.7% | 0.92 | −24.9% | 98 | 超买离场 26 笔噪声 |
| v7.4 | 超卖跌幅 10→20%；超买 2-of-4 双确认 | +82~138% | 0.7-0.85 | −13~17% | 105-131 | **严重失败**：四条件牛市恒真，空仓 60-83% |
| v7.5 | 动能消失当清仓用 | +138.8% | 0.71 | −19.3% | 81 | 空仓 40.1% |
| **v7.6** | **临时仓卖出=动能消失（J>90跌破80 / RSI>70跌破65 / 10日顶背离）+ A 态三维极值清仓 + 60 自然日兜底** | **+328.5%** | **0.97** | **−27.6%** | **43** | **定稿，已上线** |

**关键教训**：v7.4 字面执行"2-of-4 双确认卖出"导致四条件在牛市常态恒真、买入次日即卖、空仓 60-83%——用户随后澄清"该信号不是用于清仓，而是 125/150 仓位的卖出信号"，v7.6 恢复了 A 态三维极值清仓，才回到 +328.5%。

## 7. 线上产品架构

**GitHub 仓库**：https://github.com/andy-develop/red-dividend-strategy（私有，2026-09-09 创建）
- 仓库根目录 = `product/` 内容（`.github/workflows/daily.yml` 位于仓库根，Actions 才能识别）
- 本地镜像：`/home/user/Doubao/chats/38440664656484354/product/`（已 git init，remote=origin main）
- Secrets（Settings → Secrets and variables → Actions）：
  - `HSK_API_KEY` = `ph_key_0d97...6100`（HSK 文件托管 API Key）
  - `HSK_RESOURCE_ID` = `1788920564682150822`（⚠️ 原计划用 Variable，但该账号 variables API 返回 404，已改用 Secret 并经 `${{ secrets.HSK_RESOURCE_ID }}` 引用）
- 部署验证：2026-09-09 手动 `workflow_dispatch` 运行 #34314906063 成功（update.py 抓行情→状态机→生成 96KB index.html→hsk-cli 推送，日志确认 20 档抄底胜率 85%）

**update.py 每日流程（GH Actions 每天 UTC 0:00 = 京 8:00 执行）**：
1. 抓中证官网 `index-perf` 接口 H20269（全收益）/ H30269（价格），最近 3800 自然日（覆盖 2016-09-08 起点 + 指标余量），限流退避 2s+
2. 算指标（布林/周线KDJ/周线RSI/日线RSI/63日动量/超卖/超买/动能消失）
3. 状态机重放（START=2016-09-08 起），得当前状态/仓位/临时仓档倒计时/离场回补倒计时
4. 计算触发缺口（"今天再跌 X% 将触发…"）
5. 生成 index.html（模板 + PAYLOAD 注入），写入

**页面区块**：今日信号 → 买卖点触发条件与距离 → 仓位与 60 自然日倒计时 → 策略详情（回测指标/净值曲线/回撤/43 笔明细/**抄底胜率按年+按原因**）→ 策略说明。

**HSK 部署**（详见 `~/.hsk/AGENTS.md`）：
- 二进制：`~/.hsk/bin/hsk-cli-linux-amd64-v0.7.13`（`export PATH=$PATH:$HOME/.hsk/bin`）
- API Key：`~/.hsk/api_key.json`（`ph_key_0d97...6100`，file_hosting 场景自动认领通道）
- resource_id：`1788920564682150822`；公网 `https://945q5w.gicp.fun`
- 推送命令：`hsk-cli file-hosting index.html --resource-id 1788920564682150822 --format json`
- ⚠️ update 模式必报 `claimed:false/pending`（verify_code 1050）属正常，**内容即时生效**，无需处理；业务命令前先跑 `hsk-cli context wizard --format json` 建画像，一律 `--format json`

## 8. 验证方法（改动后必跑）

1. **回测重算**：`cd backtest && python3 backtest_v7.py`（写 metrics/equity_curve/trades）
2. **独立验证**：`python3 verify_v7.py` —— 指标重算、费用逐笔、仓位跳变合法性、卖出分类（5/9/5）、12 项关键交易抽查、回撤谷底 2018-10-18，全过才算数
3. **产品离线一致性**：`product/` 下用本地 `../backtest/h20269_daily.csv`（截 ≥2014-01-02）构造 df → `add_indicators` → `replay`，与 `trades.csv`（去掉 2016-09-08 期初买入）逐笔对比，必须 0 不一致（当前 42 笔 100% 吻合）
4. **页面自检**：`python3 /runtime/skills/html/scripts/shot.py index.html`，consoleErrors 必须为 0、无横向溢出；Read `_shots/` 桌面截图人工核对渲染。已知豁免：策略说明② C(125%) 相邻 `<b>` 的 overlappingText 误报（换行边界盒重叠，视觉正常）
5. **线上验证**：`curl -s https://945q5w.gicp.fun | grep "v7.6"` 确认内容已生效

## 9. 已知限制 / 未完成项

- `backtest/prep_report.py` 管线产物 `report.html` 仍为 v7.2 版，用户未要求重生成（如需要：改 report_template.html 文案后重跑）
- 125%/150% 超配隐含 25%/50% 融资，**按 0 成本建模**，实盘需评估融资成本
- 行情源为中证官网，GH Actions 运行环境若被官网 WAF 拦截需调整抓取源
- 抄底胜率"按年拆分"当前 2018 年仅 3 档（1 盈 2 亏，33.3%），样本小，勿过度解读
- 页面上 `r-sharpe`/`r-sharpe-bh` 是硬编码 0.97/0.61（与 metrics.json 一致）；`r-mdd` 由 BT 数据现算

## 10. 常用操作

```bash
# 重新生成页面（在线抓最新行情）
cd product && python3 update.py

# 推送线上
export PATH=$PATH:$HOME/.hsk/bin
hsk-cli context wizard --format json          # 建画像
hsk-cli file-hosting index.html --resource-id 1788920564682150822 --format json

# 回测 + 验证
cd backtest && python3 backtest_v7.py && python3 verify_v7.py

# 页面自检
python3 /runtime/skills/html/scripts/shot.py product/index.html
```

## 11. v7.7 审计整改（2026-09-09）

用户给出 11 条审计意见，全部属实，按"回测可复现 / 口径修正 / 过拟合体检 / 数据工程"四块整改完成：

### 11.1 做了什么
1. **统一回测引擎** `backtest/engine.py`（唯一实现）：`get_prices`（合并 TR/px）→ `build_signals`（全部信号在 **H30269 价格指数** 上算）→ `replay`（T+1 撮合 + legs FIFO）→ `equity_curve`（日频全量净值 + 费用/滑点/融资成本逐日入账）→ `metrics` / `oversold_stats`。update.py 改为 import engine 共用，**双实现漂移已消除**（离线一致性 6 项指标逐位吻合）。
2. **三处偏差修正**：
   - 成交改为 **T+1 收盘**（数据无开盘价，T+1 收盘为可执行近似，如实披露）+ 单边**滑点 5bp**
   - 杠杆按**年化 7% 按交易日计息**（125/150% 部分每日计提）
   - 信号用 px（H30269）、收益用 TR（H20269），`px` 死代码已启用
3. **日频全量不抽稀**：净值/回撤/夏普全在日频 2427+ 点上计算，抽稀仅用于页面图表展示；回测曲线随每日行情滚动重生成（`backtest_data.json` 不再冻结）。日频回撤 −32.5%（原抽稀 −27.6% 确系低估）。
4. **修 bug**：日线 RSI 的 `rd=(-delta).clip(upper=0)` 写反（应 lower=0），曾产出 `-Infinity` 使页面 JSON.parse 失败并污染顶背离信号；修复后 diverg 信号正常。
5. **过拟合体检** `backtest/scan_sensitivity.py` → `sensitivity.json`：8 参数 ±扰动（除 X_UP 外夏普波动 ≤0.05，参数平原；X_UP=20 明显优于 15/25，为唯一敏感参数）；walk-forward 三区间 0.45/0.56/0.75 无崩溃。**PBO/Deflated Sharpe 未实现**（披露）。
6. **数据工程**：`requirements.txt`、`tests/test_engine.py`（16 用例：指标自洽/状态机/T+1/净值复算/抄底闭环，全过）。多源兜底与缺失校验未做（披露）。

### 11.2 v7.7 新结果（口径变更，与 v7.6 不可直接比）
- 策略 **+148.0% / 夏普 0.61 / 回撤 −32.5%**（vs BH +134.3% / 0.62 / −27.5%）；55 笔；总成本（费+滑点+利息）13,033 元；抄底 27 档胜率 70.4%
- v7.6（+328.5%/0.97/−27.6%）基于"同日收盘成交 + 0 成本杠杆 + TR 信号"，审计确认为不可复现的乐观口径，已废弃
- 仓位分布（修复后日频）：0% 5.6 / 100% 69.4 / 125% 7.0 / 150% 18.0

### 11.3 v7.7 验证方法
1. `python3 backtest/engine.py` 产出 `metrics_v77.json / trades_v77.csv / equity_curve_v77.csv`
2. `python3 -m unittest tests.test_engine -v`（16 用例全过）
3. update.py 离线管线 vs 独立引擎 6 项指标逐位一致
4. `python3 backtest/scan_sensitivity.py` 体检
5. 页面自检 shot.py（consoleErrors 0 / 无溢出）+ 桌面/移动截图人工核对
6. 线上：推送 GitHub 触发 Actions → 覆盖推送 HSK → `curl https://945q5w.gicp.fun | grep v7.7`

### 11.4 剩余披露项（诚实清单）
- T+1 收盘成交是无开盘价的近似（非 T+1 开盘）；滑点 5bp / 融资 7% 为审计意见区间内取值
- 数据单一来源（csindex），无多源兜底与缺失校验；Actions 环境可能被 WAF 拦
- 完整 PBO / Deflated Sharpe、参数敏感性热力图（交互版）未做；X_UP 敏感需实盘注意
- index.html 仓库副本与线上版漂移（workflow 不回写）；页面依赖 jsdelivr CDN 的 echarts（被墙时图表降级为文字提示）

## 12. 新手向漏斗式仪表盘（v7.7 页面改版，2026-09-09）

用户指出原页面对小白 6 大硬伤（无"今天做什么"直白结论/指标黑话/仓位抽象含杠杆/标的是不能买的指数/排版顺序反/术语零折叠），按方案重写 index_template.html（复用 __PAYLOAD__ 结构，update.py 流水线不变）：

- **① 今天做什么**：超大结论卡，一句人话（继续满仓持有 / 今天可以加仓抄底 / 涨太多建议清仓落袋 / 把加仓部分卖掉回满仓）+ 对应 ETF 512890（注明跟踪价格指数、回测用全收益的差异）+ 按本金自动算该持有/加仓金额
- **② 市场情绪温度计**：绿→红渐变条 + 指针 + 人话档位（极度恐慌…极度贪婪），当前 81/100「极度贪婪·别追高」；底层 J/RSI/动量小字附注
  - 温度算法（可复算，不参与信号）：0.4×clip(J,0,100) + 0.4×周线RSI + 0.2×clip(100+dn63×3,0,100)
- **③ 我的仓位**：本金输入实时换算（底仓/当前持有/加仓需借/空仓现金）+ 黄色两融提示（无两融账户可只做 100% 底仓忽略加仓信号）+ 跟投三步走（开户→买 512890→每天照①操作）
- **④ 进阶全部折叠**（5 个 details）：信号明细（2-of-4 打勾+缺口）、10 年回测证据（指标卡+净值/回撤图，**展开时才 init echarts** 避免隐藏容器 0 尺寸）、术语表（黑话人话双层）、历史买卖点 55 笔、策略说明与数据口径（含温度算法公式）
- 验证：shot.py 桌面+移动 consoleErrors 0/无溢出；playwright 模拟展开 details 确认图表延迟初始化正常（canvas 各 1、无 console error）
- 已推送 f41d579，Actions #34317866200 success，线上 945q5w.gicp.fun 确认新版（A/100%、+148.0%、55 笔，details×5）

## 13. 引擎修复：warm-up 丢失与周线重采样列错误（2026-09-09，用户要求"回到 v7.6"后定位）

用户要求回到 v7.6（+328.5%）。逐项归因（attribution.py，engine 加 use_tr/t1 实验参数）发现 v7.6 与 v7.7 差异分四层：
1. **不可恢复的虚高 ~157pp**：v7.6 份额按全收益指数价格成交（现实中全收益指数不可交易，只能按价格指数≈ETF 成交）；TR/px 起点比 1.38 是最大虚高来源
2. 口径三项 ~28.5pp：T+1（6.4）、滑点+利息（12.1）、px 信号（12.8）——审计要求，保留
3. **本次修复的两个真实 bug（双实现漂移）**：
   - get_prices/load_prices 截断到 START，丢失 2014-2016 指标 warm-up（v7.6 保留）→ 早期信号失真
   - build_signals 周线 KDJ/RSI 重采样硬编码 ("px","last")，不跟随 use_tr → 与 v7.6 TR 周线不一致
4. 修复后：交易序列与 v7.6 完全一致（43 笔对照）；诚实口径（px+T+1+成本）**+145.8%/夏普0.61/回撤-32.4%/52笔**（线上 2026-09-08；本地 CSV 止 09-07 为 +143.3%）
5. 敏感性（修复引擎）：Y_DOWN=15 → +153.6%/0.62/-31.0%（唯一稳健增益，未采纳待用户确认）；X_UP 敏感（20 最优）；其余参数平原；WF 0.44/0.56/0.75 无崩溃
6. 工程改动：LOOKBACK_DAYS 3800→4800（覆盖 warm-up）；update.py main() 改 warm-up 流程（df_all 全量信号→replay(start)→截断核算）；tests 适配新签名 16 用例全过
7. 决策待用户：诚实口径接受 +145.8%，还是采纳 Y_DOWN=15（+153.6%）；v7.6 的 +328.5% 判定不可复现（含 TR 成交虚高），不恢复展示

## 14. v7.8 定稿 + 策略概览折叠项（2026-09-09）

- 采纳 Y_DOWN 20→15（上一轮归因中唯一稳健增益点）：+156.2%/0.62/−31.0%/59笔（线上 2026-09-08；本地 CSV 止 09-07 为 +153.6%）；抄底 29 档 72.4%（21盈/8亏，平均 +3.4%）
- engine 新增 overview_stats()：按年加仓/清仓频率、平均持有天数、125/150 分布，随 run() 输出；update.py 写入 backtest_data.json.backtest.overview（每日自动更新）
- 页面折叠区新增④「策略概览 · 这套策略平时怎么操作」：总体思想=满仓+超跌抄底，实测频率=每年约 2.9 次加仓（多数年份 2~4 次，2018/2021 年各 6 次）、平均持有 47 天（最长 60 天）、清仓 0.4 次/年、胜率 72.4%；预期管理文案=一年 2~4 次加仓提示、其余时间「继续满仓持有」是常态。原④历史买卖点→⑤、原⑤策略说明→⑥
- 页面验证：shot.py 桌面+移动全绿；playwright 展开全部 details 确认概览数字渲染正确、无 console error

## 15. v7.9：回补 60→90 日 + 卖出信号实验否决（2026-09-09）

**背景**：用户对 v7.8 提出两点修改：① A 态清仓后强制回补 60→90 自然日；② 卖出条件实验。

**隔离测试矩阵**（backtest/engine.py 新增 momentum_confirm 参数）：
| 回补 | 动能消失 | 收益 | 夏普 | 回撤 | 笔数 |
|---|---|---|---|---|---|
| 60 日 | 单日（v7.8 基线） | +153.6% | 0.62 | −31.0% | 59 |
| 90 日 | 单日（v7.9 定稿） | +153.5% | 0.62 | −31.0% | 58 |
| 60 日 | 连续 2 日 | +150.7% | 0.60 | −34.9% | 62 |
| 90 日 | J 回升 85 过滤 | +151.6% | 0.60 | −35.3% | 60 |

**结论**：
1. 90 日回补采纳（中性持平）：2024 案例从 06-17 强制接刀改为 07-11 超卖共振自然回补（晚 24 天带确认），0% 天数 138→175 但无损失；超卖信号优先、90 日仅兜底。
2. 连续 2 日确认否决：cross 条件（10 日最高>90 且 J≤80）在 J 持续 <80 时连续多日为真，"连续 2 日"只延迟卖出、不滤假信号；动能消失 14→10 次、更多临时仓拖到 60 日到期卖，回撤 +3.9pp。
3. J 回升 85 过滤否决：顶背离触发时 J 常在 >85（挂起被无限取消→全拖到期卖），j_cross 时 J≤80 的 5 日内几乎不可能回 85（过滤形同虚设）；卖出整体延后 4-6 天，回撤 +4.3pp。

**改动**：engine.py REBUY_DAYS=90（HOLD_DAYS=60 仅临时仓到期不变）、动能消失保持单日（momentum_confirm 实验参数保留默认 "single"）；update.py 回补日期改用 REBUY_DAYS（原误用 HOLD_DAYS，已修）；页面版本 v7.9、A 态回补文案 60→90（临时仓 60 文案不变）；product/backtest/engine.py 为真正被 update.py 加载的副本（非 product/engine.py，同步时注意）。
**结果**：+156.1%/0.63/−31.0%/58 笔（rolling 2026-09-08）；commit 03d96b7，Actions success，线上已确认。

## 16. v7.10：六项改进的隔离回测与定稿（2026-09-09）

用户提出 6 项改进，全部实现为可开关实验并隔离回测（本地 CSV 基线 +153.5%）：

| 改进 | 实现 | 结果 | 判定 |
|---|---|---|---|
| ①超卖跌幅 15→12 + 跌幅加速 | Y_DOWN 可调 + dn10≥dn63×0.6 开关 | Y12 +163.7%/0.63；Y14 **+162.9%/0.64/−30.4%**（三段WF 0.46/0.61/0.80 一致增强）；加速确认 +152.3%（负） | **采纳 Y14**（WF 稳健+夏普回撤双改善；12-15 为参数平原，取保守侧防过拟合） |
| ②强制回补 90→45 | REBUY_DAYS | +133.7%/0.57（−20pp，下跌中提前接刀） | 否决，保持 90 |
| ③动能消失 T+1→T+3 | delay_sell 参数 | +156.3% 但回撤 −32.2%，WF 无增益 | 否决，保持 T+1 |
| ④股息率估值过滤器 | 数据源核查 | 中证官网 xls 仅 20 条滚动；akshare 同源；无 2014-2026 长历史股息率 | 待独立数据工程（韭圈儿/理杏仁/自建） |
| ⑤顶背离三选二 | RSI+MACD柱+量价（H20269 量）≥2 | +142.1%/0.59/−34.2% | 否决，保持仅 RSI |

**v7.10 定稿**：Y_DOWN=14（其余参数不变）。全期 +165.6%/0.64/−30.4%/63 笔（rolling 2026-09-08；本地 CSV +162.9%）；抄底 32 档胜率 75.0%、加仓 3.2 次/年、清仓 4 次。
改动：engine.py Y_DOWN=14 + 实验开关（Y_ACC/DIV_2OF3/DELAY_SELL，全 False 保留可复现）；get_prices 带出 vol（H20269）支持量价背离实验；页面 v7.10、超卖文案≥14%；commit 8590d48，Actions success，线上已验证。

## 17. v7.11：估值剪刀差门 + 年线门（2026-09-09）

用户新增 3 项改进，全部实现为引擎开关并隔离测试：

| 改进 | 实现 | 结果（本地 CSV，基线 v7.10 +162.9%/0.64/−30.4%） | 判定 |
|---|---|---|---|
| ①估值剪刀差门 | 股息率代理(TR/px 12月滚动) − 10Y国债(akshare bond_zh_us_rate, 静态缓存 cn10y_daily.csv)；分位≥80% 正常 / 50-80% 半力禁150档 / <50% 超卖信号失效 | 单开 +154.8%/0.64；+年线 +165.5%/0.67/−31.7%/胜率90% | **采纳（与年线组合）** |
| ②年线门 | 仅 价格<250日线 时允许超卖 | **单开 +172.3%/0.67/−30.4%/胜率85.7%**，WF 0.46/0.66/0.83 三段不差于基线 | **采纳** |
| ③日周线共振 | wj<0 全仓 / wj≥0 半力或不动 | "不动"+162.9→+153.0%；"半力"+162.0%；组合均降 | **否决**（两档均测） |

**数据工程**：官方股息率仅 20 条 → 用 TR/px 比值 12 个月滚动增长率做代理（当前 4.96% vs 官方 4.52%，偏高 ~0.4pp 含再投资收益；分位形态与官方一致：2018 底 3.8%、2020-03 4.28%、2024-09 5.62%）。十年期国债 akshare 拉取 2013-2026 日频存 CSV（3421 行），update.py 读取静态缓存。
**窗口选择**：expanding/2y/3y/5y 滚动全测。5y 全期最优（+166.4%/0.68/−30.2%）但 min_periods=1008 超过回测前预热（2016-09 前仅 ~660 交易日）→ 2016-2019 段信号全禁（WF 9 笔/0.44）冷启动否决；3y（min_periods 605）回测起点前可用，WF 0.44/0.68/0.85（后两段大幅增强、首段略降因估值不便宜的机会被滤），定稿 **3y**。
**v7.11 定稿**：MA250_GATE=VAL_GATE=True（3y 分位），WEEK_J0=False。全期（rolling 09-08）**+168.2%/0.68/−31.7%/44 笔/抄底 20 档胜率 90%/加仓 2.0 次年**。
**页面**：温度计下新增"抄底门槛"行（估值分位 + 年线状态 + 拦截提示）；策略说明更新 2-of-4×双重门槛；今日 09-08 估值分位 48.5% <50%，页面演示拦截（gate_ok False）。
**坑**：expanding 与滚动窗口混用（patch 顺序）会产出不可复现混合结果（+169.2%），已重跑 clean 配置修正；5y 冷启动问题由 WF 暴露。

## 18. 每日自动更新链路可靠性审查与加固（2026-09-09）

用户要求自查"完全依赖 GitHub Actions 每天更新有没有问题"。审查结论与修复：

**已核实无问题**：
- cron "0 0 * * *"（UTC）= 北京 8:00，2026-09-09 提交，首次 schedule 触发为次日 08:00（当前全部 run 均为 dispatch，属正常）
- 中证当日数据延迟发布（16:56 实测仍无当日）→ 早 8 点取 T-1 收盘属设计；周末/节假日 data_date 停留最后交易日
- 全量抓取（4800 天窗口）设计：即使 schedule 某天被跳过/延迟，下次跑全量重建，数据不丢
- concurrency 防重叠；secrets（HSK_API_KEY/RESOURCE_ID）已配置；Actions 失败默认邮件通知仓库 owner
- hsk-cli +host 命令实测有效（v7.11 已线上生效）

**发现并修复的问题**：
1. **[严重] 数据陈旧静默**：两序列交集取数 + 无最新日期校验，接口部分返回时会静默倒退 data_date → update.py 加 validate_data（>10 天陈旧 / >3 天滞后 / 非正价 / 空值 / 单日>25% 异常），警告展示在页面顶部黄条，不静默
2. **[隐患] pandas 版本漂移炸弹**：workflow 用 `pip install pandas numpy` 不锁版本，代码含已弃用 `fillna(method=)` → 改 `-r requirements.txt` 锁 pandas<2.3/numpy<2.3，engine 改 ffill()
3. **[展示] generated_at 显示 UTC** → 统一北京时间
4. **[数据] cn10y 静态缓存永不刷新** → 新增 refresh_cn10y.py（每季度手动刷新，文档注明；缓存缺失引擎自动降级）
5. **[流程] 发布无自动验证** → workflow 加 Verify 步骤比对线上/本地 generated_at

**已知残余风险（接受/需人工）**：HSK 或中证任一外部依赖故障时页面停留旧版（有黄条警告 + Actions 邮件通知，无第三方告警）；schedule 触发不保证准时（官方机制）；cn10y 需季度人工刷新。

## 19. P0-P2 审查整改：可复现性与口径地基修复（v7.12，2026-09-09）

外部审查报告（P0-3 线上/仓库不可复现、P0-4 休市日误喊"今天买"、P0-5 每日重写历史零留痕、P1 口径/降级 bug、P2 工程脆弱点）。逐条核实与修复：

**地基级修复（所有数字重算）**：
- **[真 bug] 净值用价格指数计价**：equity_curve 此前持仓市值按 px（H30269）估值，只有买入持有用 TR——策略端漏掉全部分红再投，收益系统性低估。修复：市值 = TR 归一份额 × 全收益（q=Σ买入额/买入日TR），满仓场景与 bh 完全同口径（sanity 验证仅差 6pp 来自融资成本错配场景、真实场景一致）。v7.11 +168.2% → v7.12 **+285.3%/0.90/−29.0%/44 笔**（买入持有 +134.3%/0.62/−27.5%）。
- **[非 bug] 成本后置扣减**：nav_ts − cum_cost 与成本发生时扣除逐日数学等价，无实质影响（已核实）。
- **[真 bug] vol 被切片切掉**：get_prices 先切 [date,close] 再判 "trading_vol" in columns 恒 False → df['vol'] 全 NaN → 旧"顶背离三选二 −34.2% 否决"是在 RSI AND MACD 双 AND 失真环境下测的。解封后重测：三段 WF 与基线差 <1pp、无增益 → 维持仅 RSI 顶背离（少 2 笔），开关保留可复现，README 结论更新。

**按审查报告 10 条顺序修复**：
1. **交易日历闸门**：trade_calendar.csv（akshare sina 2013-2026 含未来）入库；休市日卡片标题改"下个交易日（X）可加仓/清仓（今日休市）"+ 蓝色休市横幅；日历缺失降级周末判断。
2. **输入落库**：workflow contents: write；data/H20269|H30269-YYYYMMDD.json + snapshot-YYYYMMDD.json 每日 commit；index.html/backtest_data.json 同步入库（当天不可变快照，出争议可还原）。
3. **固定左边界**：抓取锚定 20130719（中证最早可回溯日），不再 today−4800 每天前移；每日数据是前一天超集。
4. **抓取硬校验**：close/px 强转 float 拒 NaN/≤0、最新数据日必须是交易日且不得晚于最近交易日（防盘中占位）、重复日期拒绝、估值分位数缺失>50% 直接 raise——宁可红掉不发。
5. **os_gap 口径**：2-of-4 原 max 合成 → 逐条件列出（band/dn63/wj/wrsi 各自缺口 + 已命中数 + 还需命中数）；hi63 用明日窗口推演（max(hi63, px)）；wj/wrsi 对当日跌幅敏感性以文字提示（逐条不假装精确）。
6. **vol 解封 + DIV_2OF3 复查**：见地基级。
7. **CSV 生成脚本入库**：backtest/prep_data.py 从接口重生成两份 CSV，清洗规则固化（剔假日复制行/去重/剔 NaN/非正值）——20140101 幽灵行接口从未返回，是旧手工生成步骤造的，已消除。
8. **发布确认**：hsk-cli 输出校验（异常重试一次）、timeout-minutes、failure 通知步骤、幂等断言（同一输入跑两遍 cmp 逐位一致）。
9. **降级路径**：cn10y 缺失时补全 div_proxy/y10/spread 全 NaN（原只补三件套 → update.py KeyError job 红）；spread_pct 大面积 NaN 直接 raise（不再静默把超卖信号关成 0 后发绿页面）。
10. **时区/幂等/argv**：bj_now 北京时间（已存在）；argv 位置敏感解析改为循环解析；--dry-run 支持。

**关键验证**：
- 新 CSV（prep_data.py 重生成）与线上同源同时点：3196 行 2013-07-19~2026-09-08，幽灵行 0、重复 0、周末行 0。
- 线上全链路跑通：3197 行、校验过、+285.3%、44 笔，index.html 132KB。
- 交易日历闸门模拟：周六 → 休市 + next 周一；周五 → 交易日。
- shot.py 自检：无 console 错误/无溢出/无响应式问题；overlappingText 为 details 展开测量噪声（手动展开验证 overlap=False）。
- DIV_2OF3 WF：三段 30/66/76% vs 30/67/76%，无增益不采纳。

**数字变化说明**：v7.11 +168.2% → v7.12 +285.3% 的 +117pp 全部来自净值按 TR 计价（10 年分红再投 + 杠杆期分红）；交易序列（44 笔）与信号完全不变，仅净值核算口径修正。

## 20. 右侧目录导航改造（v7.13，2026-09-10）

用户要求页面增加可点击目录树，规划多策略产品形态。

**需求结构**：
- 一级「选ETF」→ 二级 资产配置策略 / 行业轮动策略 / 估值驱动策略 / ETF分类（三级 宽基ETF·中风险 / 行业主题ETF·高风险 / 跨境ETF·高风险）
- 一级「ETF择时」→ 二级 红利低波（现有完整仪表盘）/ 沪深300 / 中证500
- 仅红利低波有内容，其余节点一律展示得体空页（不报错、不白屏）

**实现**：
- `index_template.html`：布局改 `.layout`（flex，主内容 + 右侧 sticky 侧栏 236px）；树渲染由 JS 数据驱动（TREE 常量 + renderTree()），一级/二级可折叠（class open + 箭头旋转），叶子可点击；hash 路由 `#/timing/hongli` 等（三级取 hash 最后一段为 key）；PAGE 常量存空页文案（标题/面包屑/说明）；移动端（≤900px）侧栏变右下角悬浮按钮 + 遮罩抽屉（transform 滑入、visibility/pointer-events 隐藏态）；切回红利低波时若回测折叠区已展开则补 initCharts（避免隐藏容器 0 尺寸）。
- **PAYLOAD/数据层零改动**（目录是纯前端结构）；策略引擎/口径未动，仅页面外壳。
- 版本号 v7.12 → v7.13（页面 header）。

**验证**：
- playwright：默认路由、三级 hash（#/sel-cat/cat-broad）、沪深300、ETF分类、选ETF、非法路由回落、点击目录节点、移动端抽屉开/关——全部通过，无 console 错误。
- shot.py：桌面无 console 错误/无溢出；移动端 horizontalOverflow 报 sidebar 为 fixed 抽屉设计使然（playwright 实测 scrollWidth==innerWidth==390，抽屉开/关均无真实横向滚动）。
- 修复过的 bug：三级路由 hash 最初取整串（#/sel-cat/cat-broad → key 不匹配不生效），改为 split("/").pop() 取最后一段。

**发布**：update.py 重生成（数据滚动到 2026-09-09，+286.5%/0.90/−29.0%/44 笔——多一个交易日的正常滚动，非口径变化）→ git commit + push → dispatch daily.yml → 线上 945q5w.gicp.fun 验证（v7.13 标记 + 目录树）。

## 21. 目录移动到左侧（v7.13 修订，2026-09-10）

用户要求目录整体换到左边。改动：
- DOM：`<aside class="sidebar">` 从 main 之后移到 main 之前（flex 布局左→右，桌面目录即居左，sticky 不变）。
- 移动端抽屉反向：`right:0; translateX(102%)` → `left:0; translateX(-102%)`（从左滑入），阴影方向 `-6px → 6px`，悬浮按钮 `right:14px → left:14px`（左下角）。
- 验证：桌面 sidebar 左缘 146 < main 左缘 402（目录在左）；移动端抽屉 left 0→right 270 从左侧滑入、开/关无横向滚动（scrollWidth==390）；shot.py 桌面无错误/无溢出。

## 22. ①-⑤ 板块视觉增强（v7.13 修订，2026-09-10）

用户要求五个板块在 UI 上更醒目、超过其他网页内容，但不超过总标题。
- 总标题 h1：20px/700 → **24px/800**（保持页面最高层级）。
- 板块标题 .sec-t：13px → **15.5px/800** + 左侧渐变蓝竖条 + **蓝色编号徽章**（.sec-n 26×26 圆角块，白字 ①-⑤）。
- ① 内结论 .action-title 与 ② 温度计 .gauge-word：24px → **20px**（不越过总标题）。
- section 间距 18→22px；.card 加微阴影（0 3px 14px rgba(43,108,176,.08)）。
- 左侧目录（13.5-14px）与页脚（12px）保持不变，视觉上被 ①-⑤ 压过。
- 验证：playwright 桌面/移动字号层级 h1(30px 移动渲染) > action(20) > sec-t(15)；无 console 错误、无横向滚动。

## 23. 布局间距与目录宽度微调（v7.13 修订，2026-09-10）

用户要求目录与主内容间距放大一倍、目录本身再窄些。
- `.layout` gap 20px → **40px**（翻倍）。
- `.sidebar` flex 0 0 236px → **192px**；树内 lv2 缩进 24→18px、lv3 缩进 40→28px、lv3 字号 13→12.5px、risk 标签缩小（10.5→10px、margin 6→4px），lv3 实测无溢出。
- 主内容宽 960→916px（布局总宽 1180 不变，留白给间距）。
- 验证：桌面无 console 错误/无溢出；实测 sbW=192、gap=40、lv3Overflow=false；移动端 drawer 不受影响。

## 24. ①-⑤ 小标题再增强 + 修复未闭合 CSS 注释（v7.13 修订，2026-09-10）

用户要求小标题再醒目些、每个模块上方留空行。
- `.sec-t` 17px/800 + 浅蓝渐变胶囊底（rgba 蓝 0.12→0.03）+ 1px 蓝边；编号徽章 26→28px；`section + section{margin-top:34px}`（②③④⑤ 上方留 34px 空行，① 与 header 保持现状）。
- **【重要修复】v7.13 起存在的未闭合 CSS 注释**：`/*（640px 下的字号微调合并到下方统一 @media）` 无 `*/`，导致 `.brand` 之后**全部主样式规则**（header h1/.badge/.card 边框背景/.action-box/gauge/pos-grid/details/回测表/页脚等）被浏览器当注释吞掉——此前多轮"视觉增强"实际从未渲染（页面靠 body 默认白底 + 浏览器默认字号维持外观，h1 30px 实为默认 2em）。修复：删除该注释行及游离 `}`。实测：sec-t 17px/渐变/1px 边框、徽章 28px、h1 24px、card 边框 1px、模块间距 34px 全部生效；页面高度 1748→2082px（样式恢复的副产物）。
- 自检盲区教训：shot.py 只检 console 错误/溢出，测不出"规则被吞"；CSS 改动后必须验证 computed style 真实生效。

## 25. 行业轮动策略上线（v1.0，2026-09-10）

用户给出《ETF行业轮动量化策略方案》（豆包 docx Ort4dAO2Von3TwxRKkbcca1anpd）与线上路由 `#/sel-sector`，要求实现一版并展示在 https://945q5w.gicp.fun/#/sel-sector（同仓库、同发布通道）。已上线。

**新增/改动文件**：
- `sector_universe.py`：21 行业 × 30 ETF 标的池 + 东财 push2his 日 K（前复权/成交额/换手率）+ 中证官网 index-perf（H00300/000300）。纯 HTTP，无 akshare（CI 依赖约束）。
- `sector_engine.py`：统一引擎（因子/回测/风控/快照），与单测共用单一实现；参数严格按方案（Top-4 等权、15% 换仓门槛、前 60% 留仓、月中加速、20/60/120 日动量 50%+波动率 20%+拥挤度 20%+反转 10%、8% 熔断→50%、12% 止损、P80→70%/P90→50%、月换手 ≤100%、双边 12bp、2018 至今、沪深300全收益+行业等权基准）。
- `payload_util.py`：PAYLOAD 提取/注入，update.py 与 sector_update.py 共用（carry-forward 互不覆盖）。
- `sector_update.py`：并发抓取→硬校验（有效行业 ≥80%）→引擎→payload→合并注入 index.html→sector_data.json→data/ 归档（sector-raw-*.json.gz + sector-snapshot-*.json）。
- `update.py`：render() 支持 sector 段携带；argv 加 _DRY。
- `index_template.html`：view-sector 视图（持仓/排行/风控/回测 KPI+净值图+回撤图+分年表/调仓明细/ETF 映射/口径披露），routeTo 三分支。
- `.github/workflows/daily.yml`：update.py 之后串 sector_update.py，各带幂等断言（两次 dry-run cmp），发布自检同时比对红利低波与行业轮动两段 generated_at。
- `tests/test_sector_engine.py`：18 项单测（含换手上限防清仓/成对换仓/双重缩放回归）。

**真实数据实测（2026-09-10）**：32/32 ETF 抓取成功（含此前两度失败的 512980 传媒）、21/21 行业通过硬校验、CSI 2843 条。**回测结果（终版引擎）**：2018-01 至今，总收益 **+116.3%** / 夏普 **0.60** / 最大回撤 **-31.6%** / Calmar 0.29 / 月度胜率 48% / 537 笔 / 年换手 **4.51**（单边）/ 成本 7,039 元（10 万口径）；基准沪深300全收益 +37.5% / 行业等权 +69.9%。当前持仓（2026-09-10）：有色/煤炭/农业/消费 各 12.5%（实际暴露 50%，调仓日上限 70%）；风控：cb=False、mk_pct=0.808、mk_cap=0.7。页面如实展示。

**调试史（重要，勿重蹈）**：
1. 原始实现（CB 恢复=策略超额转正）回测仅 +1.8%/夏普 0.14/年换手 10.1——诊断后定位：熔断恢复条件在降仓后结构性不可满足（半仓/空仓时上涨行情超额持续为负）→ 现金锁定、错过反弹。修复：恢复条件改为"市场 20 日收益转正"（方案原文"直到信号恢复"）。
2. 波动率"仓位上限"逐日/止损日套用 → 5% 级抖仓；改为仅调仓日与熔断切换日生效。
3. 加速轮动在危机中会买"最不差"的负分行业（截面全负时门槛形同虚设）——已由熔断的"暂停开仓"在下跌行情中前置拦截；方案参数保留。
4. 第一轮诊断脚本用 setattr 逐变体改全局参数导致跨变体污染（后续变体带前序设置），矩阵结论失真——诊断必须逐变体独立子进程。
5. 东财接口限流（"Remote end closed connection"）→ 全局限流器 0.5s + 指数退避 2.0s×(k+1) 重试 6 次 + 并发 3 + 流水线级冷却重试（8s，最多 2 轮），实测 32/32 稳定。
6. **换手率口径 bug（重大，曾致 9/1 全卖未买）**：原按双边 Σ|Δ| 计"换手是否超 100%"→8/11 加速轮动把当月预算耗尽（计 1.0）→8/31 月度调仓预算=0 时上限块从零构造 base → 清空全部持仓。修复：换手口径=单边 Σ|Δ|/2（方案"≤100%＝最多全部换一遍"）；成本=gross×单边 6bp（每边各收）；上限块重写为模块级 `apply_turnover_cap(weights, sel, score, budget, tgt_w)`：**成对换仓**（最高分新买入 × 最低分卖出，预算不足整对则跳过），**绝不清仓、绝不超仓**。
7. **调仓日双重缩放 bug（重大）**：调仓日重建 100% 目标后，scale 仅在"数值变化"时应用——若 scale 未变，新目标绕过波动率上限直接满仓（市场过滤静默失效）；若上限块拦截（预算不足返回已缩放现状），再乘 scale 又造成双重缩放（12.5%→6.25% 且零换仓）。修复：目标=等权×desired_scale 一次成型（预算不足时成对换仓直接用缩放后目标权重），scale 分支仅处理熔断切换（比例调整）。
8. 幂等断言坑：`generated_at=bj_now()` 使两次 dry-run 逐位不同 → dry-run 模式固定为 "dry-run"，并给 daily.yml 的 dry 命令补上 `--dry-run` 参数。

**v1 限制（页面披露）**：拥挤度缺"份额变化率"第三维（东财无稳定历史份额接口，两维合成）；信号按行业篮子（成员等权）产生，实盘按 ETF 映射执行存在跟踪误差。

## 26. 增量更新机制 v1.1 + 发布链路 HSK 403 阻塞（2026-09-10 下午）

### 增量更新机制（用户要求：每次触发不要全量更新数据）
- **基线 = 最近一次全量周归档 + 其后每日增量**，重建为完整 raw（确定性）：
  - sector：`data/sector-week-<date>.json.gz`（全量，基线陈旧 >7 天或缺失时全量重抓轮换）+ `data/sector-incr-<date>.json.gz`（每日增量区间行）
  - 红利低波：`data/H20269-week-<date>.json` / `H30269-week-<date>.json` + `H20269-incr-<date>.json` / `H30269-incr-<date>.json`（中证指数无前复权问题，拼接安全）
  - 旧全量归档（sector-raw-*.json.gz / H20269-<date>.json）已 `git mv` 为周基线格式，`rebuild_*` 兼容迁移
- **每次抓取只请求 beg=基线末日+1 的增量区间**（每 ETF 几十行而非全量几千行）：
  - 前复权刻度检测：增量首日与基线末日收盘价跳变 ≥11% → 期间除权、历史刻度失效 → 该 ETF 全量兜底重抓
  - 缺口检测：增量首日与基线末日间隔 >10 自然日 → 全量兜底
  - 基线已是最新（start>今天）→ 零请求，直接沿用基线（当日 CI/本地先后触发时）
  - `SECTOR_QUICK=1`（CI）：增量失败快速回退基线行（2 次重试 + 20s 超时，封锁时 ~1 分钟失败），发布不中断
- **归档治理**：incr 保留 30、week 保留 2，滚动删除；git 树体积可控
- 单测：`tests/test_incremental.py` 7 项（合并/去重/刻度/缺口/重建/清理），总计 41 项全过；本地增量路径验证（基线已最新→沿用→32/32、21/21 有效）、幂等断言通过
- 相关 commit：c73ae0f（增量机制）、21fa6b5（QUICK 快速失败优化）
- CI 实测：增量模式下更新+幂等+commit 步骤 **~1 分钟跑完**（此前全量 + 限流要 20-40 分钟）

### 发布链路阻塞：HSK 资源 update 被禁用（需用户决策）
- **现象**：hsk-cli 0.7.13 `+host index.html --resource-id 1788920564682150822` → `403 code 11301002 "update function is disabled, please create a new resource"`。稳定复现（run 34440994250、34443871388 均在发布步骤失败，更新/commit 步骤全绿）。
- **背景**：今天 08:00 该资源更新成功 4 次（run 34422199931/34422510265/34422908865/34423464398）；中午起 update ticket 接口被禁用。非瞬时故障，疑似 HSK 侧资源策略/账号状态变化（无控制台访问权无法确认）。
- **诊断已备好待触发**：`.github/workflows/hsk-diag.yml`（workflow_dispatch）——依次：claim status / update 重试 / file-hosting-bind 认领 / bind 后重试 / 创建新资源候选 URL。**注意：该 workflow 尚未 push（本地 github.com 网络中断中）。**
- **可行选项**：
  1. HSK 控制台开启资源更新 / 重新绑定（需用户操作，URL 不变 945q5w.gicp.fun）
  2. 创建新资源（hsk-cli host 不带 --resource-id）→ 新 URL，需用户接受换地址或确认能否绑定旧域名
  3. 若 hsk-diag 的 bind 步骤能恢复 update → 自动恢复，URL 不变
- **线上现状**：945q5w.gicp.fun 仍是 08:00 发布的内容（红利低波 + 行业轮动 12:05 本地数据已 push 到仓库但未上线；页面数据日期 2026-09-10 08:58/12:05 的快照在 index.html 中但线上还是 08:00 版）。

## §27 发布链路最终闭环（2026-09-10）

**结论**：HSK update ticket 对**所有**资源/API key 全局禁用（403 11301002 "update function is disabled"），
create ticket 可用。用户已拍板"发布为新资源"。最终发布机制与线上地址：

- **线上地址**：`https://i48ya3.gicp.fun/#/sel-sector`（resource_id 1789024233933858716，创建于 CI run 34448604085）
- **发布机制（daily.yml "Publish to HSK" 步骤）**：
  1. 数据无变化跳过：比较本次构建 `snapshot.data_date|sector.data_date` 与 `data/hsk-resource.json` 持久化的 data_date，
     相同则跳过发布（不重复建资源）——generated_at 每次构建都变（bj_now）不可作信号；sha256 因 CI 构建环境差异
     也不可靠，data_date 随交易日变最稳。
  2. 有变化：先试 update 持久化资源（期望恢复）→ 403 → 自动 `hsk-cli host` 创建新资源（--api-key）→
     解析 public_url/public_resource_id（正则优先 public_url 字段，勿匹配输出中的 upload_url）→
     持久化 data/hsk-resource.json 并 commit push → verify 从持久化 URL 读取比对 data_date。
  3. set -e 陷阱：hsk-cli 403 退出码非 0 会终止步骤，所有 host 赋值必须 `|| true`。
- **verify 步骤**：比对线上/本地 `snapshot.data_date|sector.data_date`（不再比 generated_at）。
- **CI 关键凭据**：secrets HSK_API_KEY=本地 ~/.hsk/api_key.json 的 file_hosting key（ph_key_0d97c...，create 可用）；
  HSK_RESOURCE_ID=1789024233933858716。本地 hsk-cli host 也可创建（走 api_key.json 优先）。
- **验证结果**：run 34452263016 全绿（update 幂等 → commit → 发布跳过 → verify 通过）。
  线上 i48ya3 数据：持仓 有色/煤炭/农业/消费 各 12.5%，累计 +116.33% / CAGR 9.29% / 夏普 0.60 /
  回撤 -31.6% / 年换手 4.51 / 537 笔——与终版回测一致。
- **旧 URL 945q5w.gicp.fun**：HSK 侧 update 禁用无法更新，保持 08:00 旧版（无 sector 视图）；用户接受新 URL。
- **遗留注意**：每次数据变化（交易日新数据）都会创建新资源 → 新 URL；无数据变化不重复创建。
  若 HSK 恢复 update，发布步骤会自动回归 update 路径（URL 稳定）。

## §28 导航调整 + 发布信号升级（2026-09-10 15:30）

- **用户需求**：删除左侧目录「ETF分类」及子类目（宽基/行业主题/跨境）。已从 index_template.html 与 index.html 删除
  TREE 中 sel-cat 节点、PAGE 中 sel-cat/cat-broad/cat-sector/cat-cross 条目，并同步精简「选ETF·总览」文案。
  routeTo 为通用查找（findNode+PAGE），无需改动。
- **发布信号升级**：data_date 只随交易日变化，页面结构改动不会被感知 → 新增归一化内容指纹 content_sha
  （index.html 全文剔除所有 generated_at 值后的 sha256）。跳过发布条件 = data_date 与 content_sha 均匹配；
  数据变化或模板/导航改动都会触发发布。verify 仍比对 data_date（新资源与本地数据日期一致即通过）。
- **新线上资源**：https://73f9qb.gicp.fun（resource_id 1789030324819741701，本地 hsk-cli host 创建，含删菜单版页面）。
  已持久化 data/hsk-resource.json（data_date + content_sha）。CI run 34458104865 全绿：构建→commit→发布跳过→verify 通过。
- **验证**：chrome-headless-shell --dump-dom 本地确认 DOM 中无 ETF分类/宽基ETF/行业主题ETF/跨境ETF；
  线上 73f9qb curl 确认无 sel-cat，行业轮动/红利低波视图保留。

## §29 沪深300 择时视图（v8.0，2026-09-10）
用户需求：以 v7.7 红利低波四态仓位机为同构框架，新增沪深300 择时视图（timing-hs300，路由 #/timing-hs300），
3 参数按沪深300 高波动重标定，展示内容与方式仿照红利低波（快照卡片 + 净值/回撤图 + 抄底胜率 + 交易明细）。

- **引擎参数化**（backtest/engine.py v8.0）：新增 `make_params(**overrides)` 构造参数集；build_signals /
  replay / equity_curve / run 加 `p=None`（None=红利低波默认，行为逐位不变，41 项既有测试零回归）。
  `_PARAM_NAMES` 覆盖全部信号/执行参数；`P = p or sys.modules[__name__]`。
- **沪深300 参数**（hs300_update.py）：`E.make_params(X_UP=15.0, Y_DOWN=20.0, HOLD_DAYS=120)`，
  其余原样（REBUY_DAYS=90 / J<1,J>95 / RSI<35 / 布林 2σ / 杠杆上限 150% / T+1 / 滑点 5bp / 融资 7% /
  双门 VAL_GATE+MA250_GATE / VAL_WIN=3）。START=2016-09-08 与红利低波同窗口（可比）。
- **数据源**：中证 csindex index-perf —— H00300（全收益）/ 000300（价格），2013-01-01 起可用（已实测）。
  增量机制与红利低波同构：data/H00300|000300-week/incr 归档 + 基线陈旧 7 天全量 + 幂等 dry×2。
- **页面**：index_template.html 新增 view-hs300（复制 hongli 结构，id 前缀 x-）+ renderHs300()/
  initHs300Charts()（阈值 14→20 / 20→15 / 60→120，ETF 510300）；TREE 已有 timing-hs300 节点，
  PAGE["timing-hs300"] 由占位改 {t:"沪深300 择时", hs300:true}；routeTo 加 hs300 分支。
- **carry-forward 三端闭环**：update.py（带 hs300 段）↔ hs300_update.py（带 snapshot/backtest/sector 段）
  ↔ sector_update.py（带 hs300 段），本地按 CI 顺序三跑验证三段共存（段顺序任意不丢）。
- **CI（daily.yml）**：update → hs300_update（dry×2 幂等 + 正式）→ sector；git add 加 hs300_data.json；
  发布跳过 data_date 变三端 `snapshot|sector|hs300`；verify 比对三端 data_date。
- **回测结果**（2016-09-08 起，参数 15/20/120）：策略 +100.0% / 夏普 0.47 / 回撤 -36.8% / 38 笔；
  买入持有 +71.0% / -41.6%；抄底 13 档 61.5% 胜率 平均 +2.7%；当前状态 C 125%。
  估值门现状：沪深300 估值分位 33%（<50%），今日超卖信号被估值门拦截。
- **测试**：tests/test_hs300.py 8 项（make_params 默认=常量 / 3 参数覆盖生效且其余原样 /
  显式默认与 p=None 逐列一致 / X_UP=15 超买更多 / Y_DOWN=20 超卖更少 / HOLD_DAYS 透传 /
  snapshot.param 反映传入参数 / hs300_update.P 定义正确）。全量 49 项。
- **验证**：chrome-headless-shell 本地渲染 #/timing-hs300（状态 C·125%、+100.0%、抄底 13 档 61.5%、
  交易表 38 笔、估值门 33% 拦截）+ #/timing-hongli（A 态满仓）与 #/sel-sector（波动率过滤 70%）无回归；
  HTML 截图 /tmp/hs300-shots/hs300_top.png。
- **坑**：仓库根存在旧 engine.py（无 make_params），测试 import 顺序必须 BACKTEST 后插优先；
  到期卖出 reason 用 f-string 携带 HOLD_DAYS（勿硬编码 120 污染红利低波）；due 过滤用
  "卖出一档临时仓" 避免误匹配 "离场满90自然日·强制回补"。

## §30 行业轮动审计整改 v1.1（2026-09-11，18 项 P0/P1/P2 闭环）

**输入**：用户上传《行业轮动策略_数据工程回测审计报告.md》（v2 最终版，18 项问题 + §5 修复方向）。
**范围**：sector_engine / sector_update / sector_universe / sector_sensitivity（新）/ daily.yml / index_template / tests。
**已知勿重做**：报告"2015-2017 成交额 0"与"41% 截面负分"判无法复现（审计 §5 注明）。

- **P0-1 盘中半截 K 线 + 增量不自愈**：clean_intraday 在抓取后、引擎前强制清洗。
  last_closed_date 双修复：① 15:30 收盘时刻判断（原把未收盘今天当收盘日，活跃 ETF 半截行
  amount≥全天 60% 漏检）；② **以 raw.fetched_at 为参照**（盘中抓的 raw 在收盘后重放时半截行
  仍被剔）；③ _TRADE_DAYS 是 set，`[d for d in ... if d<=today]` 无序，cand[-1] 非最大日→排序修复。
  clean 规则收敛为"末日 > 最近已收盘交易日 → 剔除"（删除成交额半截检测：以 fetched_at 参照后
  盘中行必被规则 1 覆盖；对历史完整日做 amount 检测会误删真实缩量日——实测 09-10 全天成交
  仅近 5 日均 51% 的 ETF 被误剔，A/B 验证后重写）。
- **P0-2 熔断被 run_max 单调支配**：改滚动窗口 + 绝对 pp——`exc20 < run_max(近252日) - 0.08 且
  exc20<0`，或 `run_max<=0 且 exc20 < -0.08`；恢复=市场 20 日动量转正且 ≥10 日；触发需连续确认
  5 日、至少保持 10 交易日（防抖）。CB_LOOKBACK=252。修复后熔断真正按 8pp 门槛工作（此前是择时开关）。
- **P0-3 截面不等价**：反转阈值 max(0.10, 1/n_avail)（小截面名次放宽）；截面分位/掩码统一按
  已入池行业（vol_pct/crowd_pct/r5_pct 与 mom_z 同掩码）。
- **P0-4 无过拟合体检**：sector_sensitivity.py（SENS 8 参数 × 3 档 + WF 三区间 + 随机对照
  RAND_N=150 seed42）。**修复坑**：backtest 的 end 截断只截 n 不截数组 → P/R/score/pooled/csi_ret
  全量广播错位，WF 段崩——`P[:,:n]/R[:,:n]/score[:,:n]/pooled[:,:n]/csi_ret[:n]` 对齐。
  结果：真实夏普 0.558 分位 88%、p=0.120（审计 p=0.113 同量级，不显著 → 页面如实披露
  "不显著（过拟合风险，与审计结论一致）"）。页面新增"过拟合体检"折叠卡（随机对照/WF/参数表）。
- **P1-6/8**：拥挤度等截面统一入池掩码；SWAP_GAP/ACCEL_GAP 改 z 分加法（+0.15/+0.20）。
- **P1-7**：止损清仓只卖一次（非调仓日不双重计费）；P1-14：成本拆分 SLIP_BPS=5/COMM_RATE=万1/
  COMM_MIN=5（单边实际约 9bp）+ cost_sensitivity(panel,fac) 费率 ×1/2/4/6。
- **P1-9**：data_quality 换手率越界 0<turn<100（实测 2124 行，与审计吻合）+ 成交额零值 +
  近60日均额<5000万 4 只（基建ETF银华日均约 800 万）；warn 级披露不阻断。
- **P1-10**：kline_overlap_scale 替代 kline_scale_jump（重叠日 b/d 价=精确复权因子，KLINE_SCALE_TOL=0.005）。
- **P1-11**：基准补全 csi/ew 夏普、信息比率、同暴露折算（平均暴露约 63%）、avg_exposure。
- **P1-12**：annual 以上一年末为基准 + 连乘断言未取整值；P1-13：幸存者披露 32 ETF；
  P1-15：CI content_sha 比对。
- **P2-1**：snapshot risk.pending_rebalance（挂单待执行）；P2-3：validate 缺失≥3 或末日超前→硬失败；
  P2-4：_grab_em 对基线缺失 ETF 也写增量归档；P2-5：sector_universe 空响应兜底；
  **P2-5b（新发现）**：本地直跑从不调用 archive_incremental（注释谎称"抓取阶段已归档"）→
  次日 rebuild_base 缺当天增量，已修 main 补归档；P2-9：vol_pct/vol_mult 真实字段；
  P2-10：thin_series 强制含 argmin/argmax；P2-11：删死代码 csi_px。
- **回测数字（重抓 32/32 + 清洗后末日 09-10 干净面板）**：+102.1% / 夏普 0.56 / 回撤 -35.6% /
  499 笔 / 年换手 4.27 / 成本 8117 元（10 万口径）；基准沪深300 +37.5% / 等权 +69.9%；
  annual 2018 -17.8% … 2026 +2.5%；avg_exposure 69.2%；cost_sensitivity 6x 费率 0.5605。
  数据质量披露 2124 行越界 + 4 只流动性不达标 + 增量归档 sector-incr-20260911.json.gz（32+CSI2）。
- **关键调试链（+164.9% 异常根因）**：新 raw 里 2 只 ETF（516950/512760）末日 2026-09-11 盘中行
  amount 达全天 60%+ → last_closed 缺陷漏检 → 污染末日净值/信号。修复后 [收盘清洗] 剔除 2 条、
  末日分布 32/32 → 09-10。另 A/B 验证：删 512720 得 +164.9% 是"少一个老行业→截面重构"的正常
  结果，非数据有毒（512720 收益极值 ±10% 为正常 A 股涨跌停）。
- **测试**：tests 60 passed（test_sector_engine 26 / test_incremental 11 / test_engine 16 /
  test_hs300 8；clean_intraday 测试改 fetched_at 参照——盘中 09:49 剔、收盘 16:00 留）。
- **验证**：dry×2 幂等 OK；chrome headless dump-dom：体检卡渲染（88.0 分位 p=0.120）、s-date
  2026-09-10、s-risk/s-kpis/数据质量披露全在。git 140e47d push + CI run 34553077467。
- **发布**：CI 完成后验证线上（新 URL 若因 HSK 403 重建需告知用户）三端 data_date + content_sha。

## §31 策略层审计整改 v1.2（2026-09-11，行业轮动 S-1~S-8 + 沪深300 H-1~H-10）

**输入**：用户上传《策略层审计报告_行业轮动与沪深300.md》，指令"有则改之，无则加勉"。

### 已修复（实现缺陷，4 引擎文件 + 2 测试）
- **H-1 周线口径**（backtest/engine.py）：build_signals 剔除"未完成 ISO 周"末日行——
  实盘每日运行末日=今天（未完成周）命中本周 J，回测 ffill 上一周，97.7% 周中日期信号不同。
  判定用仓库根 trade_calendar.csv 二分（_load_cal/_week_completed），无日历降级 weekday>=4。
  对回测零影响（末日信号无次日可成交）。
- **H-6 MAX_POS 死参数**（engine.py）：replay 仓位档位 1.25/1.5/1.0 字面量 → 派生
  `c_pos=min(1.25,P.MAX_POS)`、`d_pos=min(1.50,P.MAX_POS)`、C→D 条件 `pos<P.MAX_POS-1e-9`；
  MAX_POS=1.0 可真正关闭杠杆（测试验证）。
- **H-4 Y_DOWN 被禁用**（hs300_update.py）：Y_DOWN=20 全样本 63 日跌幅最差仅 −21.6%、
  2018 年最差 −17.6%（触发 0 天）→ 回 14（v8.1）。实测 +104.4%/0.482 与审计完全一致。
- **S-3 换仓门槛从未绑定**（sector_engine.py）：旧实现 `if i in held: target.add(i)`
  无条件保留持仓到槽满，SWAP_GAP 0.0~0.30 逐位相同。重写三段：protected（前 60% 无条件）
  → entrants（非持仓按得分竞争、须 ≥ min_held+SWAP_GAP）→ weak_held 兜底。
- **联动修复（S-3 暴露既有缺陷）**：换手超预算时 apply_turnover_cap 以未缩放持仓替换，
  未换旧持仓保持 1.0 刻度 → 换手受限日 tgt 突破 desired_scale（实测 tgt=0.75 > 上限 0.5）。
  改为先按 desired_scale 缩放再成对替换（替换 1:1 不改变总暴露）。holdings_history 加 rebal 标记，
  测试断言 tgt≤cap 仅对调仓日成立（止损日 tgt=止损后实际暴露，由 ≤100% 断言覆盖）。

### 体检/披露（页面如实披露，不改参数）
- **S-1 邻域统计**（sector_sensitivity.py）：对行业日收益加 σ=0.005~0.20 高斯噪声、每档 5 seed，
  报告收益/夏普/回撤 P5/中位/P95 → sector-sensitivity.json（正式 n=150，随机对照 p=0.133）。
- **沪深300 敏感性**（hs300_sensitivity.py 新增）：本地归档（rebuild_hl 不联网）导出 CSV 复用
  E.run；基线 v8.1 +104.4%/0.48/-36.9%/41 笔（与正式一致）；X_UP/Y_DOWN/HOLD_DAYS/REBUY_DAYS
  扫描 + WF 三段 → data/hs300-sensitivity.json。Y_DOWN=20 → 100.0%/38 笔、14 → 104.4%/41 笔，
  与审计（100.0%/38 笔、104.4%/0.482）完全吻合（口径验证）。
- **页面披露**（index_template.html + sector_update.py disclosure）：
  S-2 标题改"实测方差占比：主项 99.6%/反转 0.3%"（删"动量50/波动20/拥挤20/反转10"宣称）；
  S-5 现金收益 1.5%/年→+126.6%、2.5%→+133.6%；H-4 超卖信号 63 日跌幅 ≥20%→≥14% +
  参数说明改 20→14；H-5 年线门近乎冗余注（仅影响 4 信号、关闭收益略升，估值门有效）；
  H-9 恒定 125%/150% 杠杆基准并列（+88.8%/0.412/-46.5% 与 +106.5%/0.428/-50.4%，结论支持策略）；
  H-10 期初无信号建仓 100% 假设 + 起点敏感性 28.8%~117.4%。
- **明确不做**（审计结论）：S-6 相关性约束（证伪）、S-8 调仓频率（非主变量）、逆波动率
  （−72.8pp 劣化）、H-2/H-3 熊市减仓路径/基础战术仓位解耦（策略结构再设计，用户未逐项授权，
  页面已披露年度仓位与指数收益相关系数 −0.684）。

### 验证与产物
- **测试**：64 passed（新增 S-3 门槛绑定、H-1 周线剔除、H-6 杠杆上限、Y_DOWN 14 断言）。
- **回测数字**：sector +102.1%/0.56/-35.6%/499 笔（S-3 修复对真实数据无行为变化——门槛路径
  未触发）；hs300 +104.4%/0.48/-36.9%/41 笔（v8.1）。
- **产物**：index.html（421KB，三端 payload 齐全）、sector_data.json、hs300_data.json、
  data/sector-sensitivity.json（含 neighborhood 6 档）、data/hs300-sensitivity.json。
- **验证**：payload_util.extract 三端 OK（sector data_date=2026-09-10、hs300 state=C pos=125%）；
  chrome headless dump-dom 渲染 +104.4%/+102.1% 正常；披露关键词全部落盘。
