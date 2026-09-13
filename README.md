# quant-hub

量化中枢：短线策略（动量 + LightGBM 黑盒）/ ETF 策略（红利低波 + 行业轮动 + 沪深300 择时）/ 个性化选股 三域合并仓。

> 施工依据：《quant-hub-合并方案》· 配套：`docs/落地手册.md`（完整落地手册）
>
> **在线合并页（LIVE）**：<https://andy-develop.github.io/quant-hub/> —— `build-publish.yml` 每交易日 08:12 / 17:35（北京）自动重发。
> 行情数据在私有仓 `quant-hub-data`（三资产类均已首灌，日级增量 data-stock-incr / data-etf-incr / data-index 运行中；短线域经 `materialize_qlab.py` 物化 → quant-lab 布局，见 `docs/handoff/README.md` 阶段C/D/E）。

## 结构

- `domains/` —— ★三域策略代码**原样并入**（方案 §1 / Phase-1）：`shortterm/`(quant-lab) · `etf/`(red-dividend-strategy) · `selected/`(stock-factor-engine)。策略口径零改动，只补 conftest/requirements/最小测试。详见 `domains/README.md`
- `common/` —— 三域共用的数据契约、交易日历、聚合器、门禁（覆盖率硬门禁）、限流兜底、腾讯行情 vendor（含 §0.4 沪市覆盖故障修复）
- `web/` —— 三域前端合并为单页壳（CSS 作用域隔离、涨跌色逐域冻结、零 CDN 依赖）；`python -m web.build --src domains` **自包含构建**，无需再检出三个老仓
- `common/payload/` —— 三域 payload 适配器（6 变体，含方案漏列的量化黑盒线）
- `tools/` —— 运营工具（Phase 5/6）：`publish_hsk.py` HSK 发布状态机（禁用自动换资源 / pending 不报错 / 回读校验指纹）· `retention.py` 周度封存+过期+体积报告 · `runlog.py` 各域 runlog 台账 · `repo_health.py` 周巡检 · `check_docs.py` AUTO-KPI 生成/校验
- `state/` —— CI 中间态账本（carry-forward payload / HSK 指纹 / 各域 runlog，入 git 可回滚，见 `state/README.md`）
- `tests/` —— 合并层 243 项测试（含 §0.4 事故回归锁）；三域测试在 `domains/*/tests`（CI 每域独立进程跑）

## 快速开始

```bash
pip install -r common/requirements.txt
python -m pytest tests/ -q                     # 合并层 243 项全绿

# 三域测试：每域独立进程（各自都有 engine.py 等同名模块，不能一把梭）
python -m pytest domains/shortterm/tests -q    # 短线 52 项（缺 data/ 的产物测试干净 skip）
python -m pytest domains/etf/tests -q          # ETF 64 项
python -m pytest domains/selected/tests -q     # 个性化 11 项（纯标准库、离线）

python -m web.build --src domains --out web/dist/index.html   # 自包含单页壳
```

## 数据与代码分离

行情数据在私有仓 `quant-hub-data`（Phase-2）。数据契约冻结于 `common/store/schema.py`（`CONTRACT_VERSION=1.0`），改动需评审。

## 运营自动化（Phase 5/6）

| 流水线 | 触发 | 职责 |
|---|---|---|
| `data-retention.yml` | 周六 19:00 UTC | 周度封存+过期（个股 1250 / ETF 2430 交易日，`retention.py`）+ 数据仓体积报告（700MB warn / 900MB error） |
| `repo-health.yml` | 周一 01:01 UTC | 巡检最近 5 交易日各域 runlog + payload 体积 + 数据仓体积，异常自动开 issue（去重） |
| `strategy-pm.yml` / `build-publish.yml` | 交易日 | 各域链路末尾写 `state/<d>/runlog/` 台账（`runlog.py`），并校验 README AUTO-KPI 与 payload 一致（`check_docs.py --check-only`） |


## 硬性约定（踩过坑的，勿动）

1. volume 单位 = 股；成交额腾讯口径万元 → 元（×1e4）
2. 代码归一必须在 fixup 覆盖**之前**
3. hfq 历史永久冻结
4. 覆盖率门禁**分市场**设下限（整体 55% 掩不住沪市 1.7%）
5. 个性化选股域的涨跌色是绿涨红跌（原样保留，禁止"统一"）

## 策略 KPI（AUTO-KPI）

<!-- AUTO-KPI:START (check_docs.py 生成, 严禁手改; 权威数字来自 state/<d>/payload) -->

**短线策略 KPI**：

| 指标 | 数值 |
|---|---|
| 动量开·收益 | +111.71% |
| 动量开·回撤 | -13.63% |
| 动量开·夏普 | 1.44 |
| 动量开·笔数 | 460 |
| 动量开·胜率 | 42.17% |
| 动量开·最后权益 | ¥2,117,109 |
| 动量关·收益 | +47.65% |
| 动量关·回撤 | -39.21% |
| 动量关·夏普 | 0.58 |
| 动量关·笔数 | 853 |
| 动量关·胜率 | 41.38% |
| 动量关·最后权益 | ¥1,476,544 |
| 黑盒开·收益 | +111.71% |
| 黑盒开·回撤 | -13.63% |
| 黑盒开·夏普 | 1.44 |
| 黑盒开·笔数 | 460 |
| 黑盒开·胜率 | 42.17% |
| 黑盒开·最后权益 | ¥2,117,109 |
| 黑盒关·收益 | +47.65% |
| 黑盒关·回撤 | -39.21% |
| 黑盒关·夏普 | 0.58 |
| 黑盒关·笔数 | 853 |
| 黑盒关·胜率 | 41.38% |
| 黑盒关·最后权益 | ¥1,476,544 |

**ETF策略 KPI**：

| 指标 | 数值 |
|---|---|
| 红利低波·总收益 | +286.53% |
| 红利低波·年化 | +15.06% |
| 红利低波·夏普 | 0.90 |
| 红利低波·回撤 | -29.01% |
| 红利低波·笔数 | 44 |
| 红利低波·基准收益 | 135.05% |
| 红利低波·基准夏普 | 0.62 |
| 红利低波·基准回撤 | -27.53% |
| 行业轮动·总收益 | +104.00% |
| 行业轮动·年化 | +8.56% |
| 行业轮动·夏普 | 0.56 |
| 行业轮动·回撤 | -35.65% |
| 行业轮动·笔数 | 499 |
| 沪深300·总收益 | +104.38% |
| 沪深300·年化 | +7.70% |
| 沪深300·夏普 | 0.48 |
| 沪深300·回撤 | -36.91% |
| 沪深300·笔数 | 41 |

**个性化选股**：无 KPI（payload 缺失或该域无基线）。

> 由 `tools/check_docs.py` 生成；`--check-only` 在 CI 中校验与 payload 一致，严禁手改。
<!-- AUTO-KPI:END -->
