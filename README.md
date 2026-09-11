# quant-hub

量化中枢：短线策略（动量 + LightGBM 黑盒）/ ETF 策略（红利低波 + 行业轮动 + 沪深300 择时）/ 个性化选股 三域合并仓。

> 施工依据：《quant-hub-合并方案》· 配套：`docs/落地手册.md`（完整落地手册）

## 结构

- `common/` —— 三域共用的数据契约、交易日历、聚合器、门禁（覆盖率硬门禁）、限流兜底、腾讯行情 vendor（含 §0.4 沪市覆盖故障修复）
- `web/` —— 三域前端合并为单页壳（CSS 作用域隔离、涨跌色逐域冻结、零 CDN 依赖）
- `common/payload/` —— 三域 payload 适配器（6 变体，含方案漏列的量化黑盒线）
- `tools/` —— HSK 发布状态机（禁用自动换资源 / pending 不报错 / 回读校验指纹）
- `tests/` —— 243 项测试（含 §0.4 事故回归锁）

## 快速开始

```bash
pip install -r common/requirements.txt
python -m pytest tests/ -q          # 243 项全绿
python -m web.build --src <三仓检出目录> --out web/dist/index.html
```

## 数据与代码分离

行情数据在私有仓 `quant-hub-data`（Phase-2）。数据契约冻结于 `common/store/schema.py`（`CONTRACT_VERSION=1.0`），改动需评审。

## 硬性约定（踩过坑的，勿动）

1. volume 单位 = 股；成交额腾讯口径万元 → 元（×1e4）
2. 代码归一必须在 fixup 覆盖**之前**
3. hfq 历史永久冻结
4. 覆盖率门禁**分市场**设下限（整体 55% 掩不住沪市 1.7%）
5. 个性化选股域的涨跌色是绿涨红跌（原样保留，禁止"统一"）
