"""日 → 周 → 月 聚合器（方案 §2.7）。

这是"大盘多周期 K 线"的正确性核心，不是优化项。

## 为什么必须物化入库而不让上层现算

上层各自重采样就会有多种口径 —— ETF 域 §13 正是这么踩坑的：`build_signals`
周线重采样**硬编码** `("px","last")`，不跟随 `use_tr`，与 v7.6 的 TR 周线不一致。
**物化一份 + 一个聚合器 = 口径唯一**。

同时仍标 `derived:true`，因为：① 体积紧张时可删了重建；② 疑似出错时能"重算并比对"
自证；③ 契约上明确 daily 才是事实源，防止有人去改 weekly 分区。

## 聚合口径（全部参数化、禁止硬编码）

| 项            | 规则                                              | 依据 |
|---------------|---------------------------------------------------|------|
| 周起点        | **ISO 周（周一起）**                              | ETF 域 `engine.py:79` 注释即 ISO 周；按周五对齐会差一根 |
| 月            | `resample("ME")`                                  | `sector_engine.py:544` 已用 `ME`。**`M` 在 pandas 2.2 弃用、3.0 移除** —— 统一 `ME` 避免短线域(3.0.5)与 ETF 域(<2.3)读同一份 weekly 时口径分叉 |
| OHLC          | open=区间首日 open；high=max；low=min；close=末日 close | 标准 |
| volume/amount | Σ 求和                                            | ★ 指数 amount 有的源返回 NaN → **求和后仍为 NaN，不得填 0**（0 会被上层当成"当天零成交"） |
| 被聚合的列    | 参数传入，默认 `close`                            | §13 教训：硬编码列名 → 双实现漂移 |
| 未完成 bar    | 加 **`is_partial: bool`** 列，用交易日历判定      | ★ 正确性；ETF 域实测不剔除会有 **97.7% 周中日期信号错位** |
| warm-up       | 聚合前确保输入 daily 覆盖 ≥ 目标窗口 + 前置余量   | §13 教训：截断到 START 丢失 warm-up → 早期信号失真 |

## is_partial 的正确性代价（ETF 域 §31 H-1 整改原文）

> build_signals 剔除「未完成 ISO 周」末日行 —— 实盘每日运行末日=今天（未完成周）
> 命中本周 J，回测 ffill 上一周，**97.7% 周中日期信号不同**。判定用仓库根
> `trade_calendar.csv` 二分（`_load_cal`/`_week_completed`），无日历降级 `weekday>=4`。

所以三条硬要求：
1. 每根 bar 带 `is_partial`
2. `load(freq="weekly")` 默认 `closed_only=True`
3. **判定必须用交易日历，不允许 `weekday>=4` 降级**（本模块强制：无日历直接抛错）
"""

from __future__ import annotations

import datetime as _dt
from typing import Iterable, Sequence

from .calendar import TradeCalendar, iso_week_key, month_key
from .store.schema import AGGREGATOR_VERSION, PARTIAL_FLAG

__all__ = ["aggregate", "aggregate_index_frame", "AGGREGATOR_VERSION", "AggregationError"]

# 默认聚合列口径（参数化，可覆盖）
DEFAULT_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
    "amount": "sum",
}
# ★ amount 求和后仍为 NaN，不得填 0
NAN_PRESERVING = ("volume", "amount")


class AggregationError(RuntimeError):
    pass


def aggregate(
    df,
    freq: str,
    *,
    calendar: TradeCalendar,
    cols: Sequence[str] | None = None,
    asof: _dt.date | str | None = None,
    warmup_days: int | None = None,
    pd=None,
):
    """把 daily 帧聚合为 weekly / monthly。

    参数
    ----
    df       : 必须含 code / date / open / high / low / close（volume/amount 可选）
    freq     : 'weekly' | 'monthly'
    calendar : ★ 交易日历，必需。禁止 weekday>=4 降级（长假会让周线连续错一整周）
    cols     : 参与聚合的列，默认 open/high/low/close/volume/amount 中实际存在的
    asof     : **判定 is_partial 的基准日**（= 抓取日/今日），默认取 df 的最后一个日期。
               ★ 注意与"周期末日"的区别：asof=抓取当日，周期末日=该周期最后一个交易日。
               周五收盘后抓取时两者相同（该周完整）；周三盘中抓取时 asof < 周日
               （该周不完整）。长假场景下必须用它而非 weekday 判断。
    warmup_days : 若给出，断言输入覆盖 >= 目标窗口 + warmup_days

    返回
    ----
    与输入同 code 粒度、按周期聚合的 DataFrame，含：
      code, date（周期末日）, open, high, low, close, volume, amount,
      period_key（ISO 周 'YYYY-Www' 或自然月 'YYYY-MM'）, is_partial
    """
    if freq not in ("weekly", "monthly"):
        raise AggregationError(f"aggregate supports weekly/monthly, got {freq!r}")
    if calendar is None:
        raise AggregationError(
            "aggregate requires a TradeCalendar —— 判定 is_partial 必须用交易日历，"
            "禁止 weekday>=4 降级（ETF 域实测不剔除未完成 ISO 周会让 97.7% 周中日期信号错位）"
        )
    if pd is None:
        import pandas as pd  # noqa: PLC0415

    if df is None or len(df) == 0:
        raise AggregationError("empty input frame")

    df = df.copy()
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df["date"] = pd.to_datetime(df["date"])

    if warmup_days:
        _assert_warmup(df, freq, warmup_days, asof, pd)

    use_cols = _resolve_agg_cols(df, cols)
    if asof is None:
        asof = df["date"].max()
    asof = _as_date(asof)

    out_frames = []
    for code, g in df.groupby("code", sort=True):
        out_frames.append(_agg_one(g, code, freq, use_cols, calendar, asof, pd))
    out = pd.concat(out_frames, ignore_index=True)

    # 数值列类型稳定（避免 int/float 抖动破坏逐位一致）
    for c in ("open", "high", "low", "close", "amount"):
        if c in out.columns:
            out[c] = out[c].astype("float64")
    if "volume" in out.columns:
        out["volume"] = out["volume"].astype("float64")
    out[PARTIAL_FLAG] = out[PARTIAL_FLAG].astype(bool)
    return out.sort_values(["code", "date"], kind="stable").reset_index(drop=True)


def _agg_one(g, code, freq: str, use_cols: dict[str, str],
             calendar: TradeCalendar, asof: _dt.date, pd):
    g = g.sort_values("date")
    idx = pd.DatetimeIndex(g["date"])

    if freq == "weekly":
        # ★ ISO 周：pd.Grouper(freq="W-MON") 的锚点语义正确（label=左边界周一）
        #   显式用 period 分组，避免不同 pandas 版本 W 别名语义漂移
        keys = pd.Series([iso_week_key(d) for d in idx], index=idx)
    else:
        # ★ "ME" 而非 "M"："M" 在 pandas 2.2 弃用、3.0 移除
        keys = pd.Series([month_key(d) for d in idx], index=idx)

    sub = g.copy()
    sub["_pk"] = keys.values
    agg_map = {c: how for c, how in use_cols.items() if c in sub.columns}

    rows = []
    for pk, chunk in sub.groupby("_pk", sort=True):
        row = {"code": code, "period_key": pk}
        for c, how in agg_map.items():
            s = chunk[c]
            if how == "sum":
                # ★ NaN 保持：全 NaN 求和仍为 NaN，不得 fillna(0)
                row[c] = s.sum(min_count=1)
            elif how == "first":
                row[c] = s.iloc[0]
            elif how == "last":
                row[c] = s.iloc[-1]
            elif how == "max":
                row[c] = s.max()
            elif how == "min":
                row[c] = s.min()
            else:
                raise AggregationError(f"unknown agg {how!r} for column {c!r}")

        # 周期末日 = 该周期内最后一个**实际有数据**的交易日
        last_d = max(chunk["date"])
        row["date"] = last_d
        # ★ is_partial：该周期在交易日历里是否还有未到的交易日
        row[PARTIAL_FLAG] = calendar.is_partial_period(pk, freq, asof)
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=["code", "date", "period_key", PARTIAL_FLAG])
    out = pd.DataFrame(rows)
    front = ["code", "date", "period_key", PARTIAL_FLAG]
    rest = [c for c in out.columns if c not in front]
    return out[front + rest]


def aggregate_index_frame(df, code: str, freq: str, *, calendar, asof=None,
                          cols=None, pd=None):
    """单标的便捷封装（大盘 sh000001 / sz399001 的用法）。"""
    if pd is None:
        import pandas as pd  # noqa: PLC0415

    sub = df[df["code"].map(lambda x: _norm(x)) == _norm(code)].copy()
    if sub.empty:
        raise AggregationError(f"no rows for code={code!r} in input frame")
    out = aggregate(sub, freq, calendar=calendar, cols=cols, asof=asof, pd=pd)
    return out.drop(columns=["period_key"], errors="ignore")


# ---------------------------------------------------------------------------
# warm-up 断言
# ---------------------------------------------------------------------------
# §13 教训：`load_prices` 截断到 START 丢失 2014-2016 warm-up → 早期信号失真，
# 最终把 LOOKBACK_DAYS 从 3800 提到 4800。
_MIN_DAYS = {"weekly": 5, "monthly": 21}


def _assert_warmup(df, freq: str, warmup_days: int, asof, pd):
    need = _MIN_DAYS[freq] + warmup_days
    have = df["date"].nunique()
    if have < need:
        raise AggregationError(
            f"insufficient warm-up for {freq}: need >= {need} distinct trade days "
            f"({_MIN_DAYS[freq]} for the period + {warmup_days} warm-up), got {have}. "
            f"聚合前必须确保输入 daily 覆盖 >= 目标窗口 + 前置余量（§13 教训）"
        )


def _resolve_agg_cols(df, cols) -> dict[str, str]:
    if cols is None:
        cols = [c for c in DEFAULT_AGG if c in df.columns]
    out = {}
    for c in cols:
        if c not in df.columns:
            continue
        out[c] = DEFAULT_AGG.get(c, "last")
    if "close" not in out:
        raise AggregationError("input frame must contain 'close'")
    return out


def _as_date(d) -> _dt.date:
    if isinstance(d, _dt.datetime):
        return d.date()
    if isinstance(d, _dt.date):
        return d
    if isinstance(d, str):
        return _dt.date.fromisoformat(d[:10])
    if hasattr(d, "date"):
        return d.date()
    raise TypeError(f"cannot coerce {d!r}")


def _norm(c: str) -> str:
    from .store.reader import normalize_code  # noqa: PLC0415

    return normalize_code(c)


# ---------------------------------------------------------------------------
# 派生一致性断言（manifest 层面）
# ---------------------------------------------------------------------------
def assert_derived_consistent(derived_manifest_part: dict, daily_sha: str) -> None:
    """weekly/monthly 的 `derived_from.sha256` 必须等于当前 daily 分区 sha256。

    不等 = daily 更新了但周月K没重算 → 红（方案 §2.7 门禁 / 风险 R17）。
    上层会读到"日K到今天、周K到上周"的半套数据，不报错、算出错。
    """
    d = (derived_manifest_part or {}).get("derived_from") or {}
    got = d.get("sha256")
    if not got:
        raise AssertionError(
            "derived partition manifest missing derived_from.sha256 "
            "—— 无法证明派生一致性（方案 §2.7 门禁）"
        )
    if got != daily_sha:
        raise AssertionError(
            f"derived staleness detected: derived_from.sha256={got} != daily.sha256={daily_sha}. "
            f"daily 已更新但 weekly/monthly 未重算 —— 聚合与抓取必须放在同一 job 的同一步序列里"
        )
