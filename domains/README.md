# domains/ · 三域策略代码（合并层）

> 铁律：**只做工程层合并，策略口径零改动**。本目录下三个子域是三份老仓的
> **原样迁入**，只补齐了 `conftest.py` / `requirements.txt` / 最小测试，
> **没有改任何一行策略参数、信号定义或回测口径**。

| 子域 | 目录 | 来源老仓 | 内容 | 依赖 |
|---|---|---|---|---|
| 短线策略 | `shortterm/` | `andy-develop/quant-lab` | 动量轮动 + LightGBM 黑盒（日频，日选约 10 支、持仓 5–10 天） | py3.13 · `pandas==3.0.5` `numpy==2.5.2` lightgbm baostock pyarrow |
| ETF 策略 | `etf/` | `andy-develop/red-dividend-strategy` | 红利低波 / 沪深300 / 中证500 择时 + 行业轮动选ETF | py3.12 · `pandas>=2.0,<2.3` `numpy>=1.24,<2.3` |
| 个性化选股 | `selected/` | `andy-develop/stock-factor-engine` | 全A 因子画像 + 智能选股页 | py3.11 · 纯标准库 |

## 为什么"一行不改"就能跑

三个域的脚本内部都用**扁平模块名 + 运行期 `sys.path` 注入**互相引用，
不存在跨目录包 import，所以迁入 `domains/` 后无需改 import 前缀：

- `shortterm/`：自带 `conftest.py`，把 `scripts/` 挂上 `sys.path` → `import engine` / `import signals` 照常。
- `etf/`：测试文件自己在模块头 `sys.path.insert(...)`，并**刻意让 `backtest/` 后插（优先级更高）**，
  以区分域内两个同名 `engine.py`（根目录红利引擎 vs `backtest/` 统一回测引擎）。**不要给它加 conftest**，会打乱这个顺序。
- `selected/`：新增 `conftest.py` 把 `scripts/` 挂上路径，供新增最小测试直接 `import compute_factors`。

## ⚠️ 必须分域跑测试（不能一把梭）

三个域各自都有 `engine.py` / `signals.py` 等同名模块。若在**同一个 pytest 进程**里
同时收集 `domains/shortterm` 与 `domains/etf`，`import engine` 会命中错误的域。
因此 CI 用 **matrix，每域一个独立进程 + 独立 venv**（见 `.github/workflows/ci.yml`）：

```bash
# 每域独立跑（各自 venv，见各自 requirements.txt）
pytest domains/shortterm/tests -q     # 52 项：产物依赖项在缺 data/ 时干净 skip
pytest domains/etf/tests -q           # 64 项：unittest，pytest 直接收集，一行不改
pytest domains/selected/tests -q      # 11 项：§9.1 新增最小测试（纯标准库、离线）
pytest tests/ -q                      # 合并层基础设施 243 项（common/web/tools）
```

## 数据依赖

- `shortterm/` 与 `etf/` 的**产物依赖测试**（读 `data/kline/*.parquet`、`data/meta/*.parquet`）
  在本地缺数据时**自动 skip**，不 fail。真实行情数据在 **`quant-hub-data`（私有仓）**，
  经 `bootstrap.py` sparse-checkout 挂载到 `data/`，**不进本代码仓**。
- `selected/data/prices.json`、`factors.json` 目前是空对象 —— 这是 §9.1 记录的
  "个性化域数据链路实际上是坏的"现状。合并期**不动它**（改从统一 hfq 库派生是 Phase-4 事项）。
- **合并期唯一的测试层工程适配**：给 `shortterm/tests/test_report_payload.py::test_st_banner_type`
  加了"缺 `data/meta/st_history.parquet` 则 skip"的守卫（与其 `modes` fixture 同口径）。
  该用例校验的是"ST 数据存在时的新鲜度横幅文案"，未挂载 data/ 时 `_st_banner_html()`
  返回的是合法的"数据文件缺失"第三态。**不改断言、不改策略**，data/ 挂载后照常跑。

## 构建合并页（自包含，无需再检出三个老仓）

```bash
python -m web.build --src domains --out web/dist/index.html
#   → 单文件 ~1.2MB，echarts 内联、零 CDN 依赖、三域 CSS 作用域隔离
```

## 各域台账（防翻案，原样保留）

- `shortterm/HANDOFF.md` —— 含 §5「关键 A/B 决策记录」，十几次已否决实验的精确数字
- `etf/HANDOFF.md` —— 含 §19/§30/§31 三份独立审计整改（18 项 P0–P2），
  包括"净值按全收益再投计价"这类让结果从 +168% 修正到 +285% 的地基级修复

> 已否决实验索引见 `docs/handoff/README.md`。任何参数变更需走同等强度 A/B。
