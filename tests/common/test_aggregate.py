"""聚合器单测 —— Phase 2 门禁（方案 §7.2 Phase 2 门禁 ②）。

覆盖七项聚合口径 + is_partial 正确性：

1. ISO 周边界（周一起）
2. 月边界（ME）
3. is_partial 判定**用交易日历**（不是 weekday>=4）
4. amount 求和保持 NaN，不填 0
5. warm-up 前置充足
6. 重跑逐位一致（确定性）
7. 参数化列口径（不硬编码）

外加：ETF 域 H-1 整改的等价验证（未完成 ISO 周必须能被剔除）。
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

import pytest

pd = pytest.importorskip("pandas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from common.aggregate import (  # noqa: E402
    AGGREGATOR_VERSION,
    AggregationError,
    aggregate,
    assert_derived_consistent,
)
from common.calendar import (  # noqa: E402
    TradeCalendar,
    iso_week_bounds,
    iso_week_key,
    month_key,
)
from common.store.schema import PARTIAL_FLAG  # noqa: E402


# ---------------------------------------------------------------------------
# 测试用日历：构造一个可控的交易日集合（含长假，用于 is_partial 边界）
# ---------------------------------------------------------------------------
def make_calendar(days):
    return TradeCalendar(days, source="test")


def weekdays(start, end):
    """生成 [start, end] 内所有周一~周五（简化：不排除节假日）。"""
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += _dt.timedelta(days=1)
    return out


def make_daily(dates, code="sh000001", close=None, amount=None):
    n = len(dates)
    close = close if close is not None else [100.0 + i for i in range(n)]
    return pd.DataFrame({
        "code": [code] * n,
        "date": pd.to_datetime(dates),
        "open": [float(c) - 1.0 for c in close],
        "high": [float(c) + 2.0 for c in close],
        "low": [float(c) - 3.0 for c in close],
        "close": [float(c) for c in close],
        "volume": [1000 + i for i in range(n)],
        "amount": amount if amount is not None else [1e6 + i for i in range(n)],
    })


# ---------------------------------------------------------------------------
# 1. ISO 周边界
# ---------------------------------------------------------------------------
def test_iso_week_is_monday_based():
    """ISO 周的起点是周一；按周五对齐会差一根。"""
    # 2026-09-07 是周一
    assert _dt.date(2026, 9, 7).weekday() == 0
    start, end = iso_week_bounds("2026-W37")
    assert start == _dt.date(2026, 9, 7), f"got {start}"
    assert end == _dt.date(2026, 9, 13)
    assert start.weekday() == 0


def test_iso_week_key_crosses_year():
    """跨年周的 ISO 归属（2027-01-01 属 2026-W53）。"""
    d = _dt.date(2027, 1, 1)
    key = iso_week_key(d)
    # ISO 周可能把 1 月初归到上一年
    y, w, _ = d.isocalendar()
    assert key == f"{y}-W{w:02d}"


def test_weekly_aggregation_groups_by_iso_week():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 25))
    cal = make_calendar(days)
    df = make_daily(days)
    out = aggregate(df, "weekly", calendar=cal, asof=_dt.date(2026, 9, 25))

    # 9/7(一)~9/11(五) = W37；9/14~9/18 = W38；9/21~9/25 = W39
    assert list(out["period_key"]) == ["2026-W37", "2026-W38", "2026-W39"]
    assert len(out) == 3
    w37 = out.iloc[0]
    assert w37["open"] == float(df.iloc[0]["open"])
    assert w37["close"] == float(df.iloc[4]["close"])
    assert w37["high"] == float(df.iloc[:5]["high"].max())
    assert w37["low"] == float(df.iloc[:5]["low"].min())


# ---------------------------------------------------------------------------
# 2. 月边界（ME，不是 M）
# ---------------------------------------------------------------------------
def test_monthly_uses_ME_not_M():
    """'M' 在 pandas 2.2 弃用、3.0 移除 —— 必须用 ME 语义（自然月末）。"""
    days = weekdays(_dt.date(2026, 8, 1), _dt.date(2026, 9, 30))
    cal = make_calendar(days)
    df = make_daily(days)
    out = aggregate(df, "monthly", calendar=cal, asof=_dt.date(2026, 9, 30))
    assert list(out["period_key"]) == ["2026-08", "2026-09"]
    aug = out.iloc[0]
    assert aug["date"] == pd.Timestamp(days[-1] if days[-1].month == 8
                                       else max(d for d in days if d.month == 8))
    assert aug["date"].month == 8
    assert out.iloc[1]["date"].month == 9


def test_month_key_format():
    assert month_key(_dt.date(2026, 9, 11)) == "2026-09"
    assert month_key(_dt.date(2026, 12, 1)) == "2026-12"


# ---------------------------------------------------------------------------
# 3. ★ is_partial 必须用交易日历判定
# ---------------------------------------------------------------------------
def test_is_partial_true_for_incomplete_week():
    """is_partial 的判定基准是「抓取日 asof」，不是数据末日。

    - 周五收盘后抓取（asof=周五）-> 该周完整
    - 周三抓取（asof=周三）-> 该周未走完
    """
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    cal = make_calendar(days)
    df = make_daily(days)
    # asof = 周五收盘后：该 ISO 周（周日结束）内已无更晚交易日 -> 完整
    out = aggregate(df, "weekly", calendar=cal, asof=_dt.date(2026, 9, 11))
    assert not bool(out.iloc[0][PARTIAL_FLAG]), "周五收盘后，该周应视为完整"

    # asof = 周三（盘中/周三收盘后）-> 本周尚未走完
    days2 = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal2 = make_calendar(days2)
    df2 = make_daily(days2)
    out2 = aggregate(df2, "weekly", calendar=cal2, asof=_dt.date(2026, 9, 9))  # 周三
    w37 = out2[out2["period_key"] == "2026-W37"]
    assert bool(w37.iloc[0][PARTIAL_FLAG]), "周三视角下，本周未走完 -> is_partial 必须为 True"


def test_is_partial_uses_calendar_not_weekday():
    """★ 关键差异：长假期间，"周三"也可能是本周最后一个交易日。

    用 weekday>=4 降级判断会出错；用交易日历才正确。
    构造：某 ISO 周只有周一到周三有交易（周四周五为长假），
    asof = 周三收盘 —— 日历里本周已无更晚交易日 -> 完整。
    而 weekday>=4 会认为"还没到周五"-> 误判 partial（或反之）。
    """
    mon = _dt.date(2026, 10, 5)  # 周一
    days = [mon, mon + _dt.timedelta(days=1), mon + _dt.timedelta(days=2)]  # 周一~周三
    cal = make_calendar(days)
    df = make_daily(days)
    asof = days[-1]  # 周三收盘后抓取
    out = aggregate(df, "weekly", calendar=cal, asof=asof)
    wk = iso_week_key(mon)
    row = out[out["period_key"] == wk].iloc[0]
    assert not bool(row[PARTIAL_FLAG]), (
        "该周最后一个交易日是周三（长假）—— asof=周三时按日历判定应为完整；"
        "若按 weekday>=4 会误判为未走完"
    )


def test_is_partial_month_end_holiday():
    """月末长假：某自然月最后一个交易日是 28 日（29~31 为假期）。"""
    y, m = 2026, 1
    days = [d for d in weekdays(_dt.date(y, m, 1), _dt.date(y, m, 31))
            if d.day <= 28]
    cal = make_calendar(days)
    df = make_daily(days)
    out = aggregate(df, "monthly", calendar=cal, asof=_dt.date(2026, 1, 28))
    row = out.iloc[0]
    assert row["period_key"] == "2026-01"
    assert not bool(row[PARTIAL_FLAG]), "月末长假：asof=该月最后交易日后应视为完整"


def test_partial_requires_calendar():
    """无日历直接抛错，禁止 weekday>=4 降级。"""
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    df = make_daily(days)
    with pytest.raises(AggregationError, match="TradeCalendar"):
        aggregate(df, "weekly", calendar=None)


# ---------------------------------------------------------------------------
# 4. ★ amount 求和保持 NaN，不填 0
# ---------------------------------------------------------------------------
def test_amount_sum_preserves_nan():
    """指数 amount 有的源返回 NaN -> 求和后仍为 NaN，不得填 0。

    0 会被上层当成"当天零成交"。
    """
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    cal = make_calendar(days)
    n = len(days)
    amount = [float("nan")] * n
    df = make_daily(days, amount=amount)
    out = aggregate(df, "weekly", calendar=cal, asof=days[-1])
    val = out.iloc[0]["amount"]
    assert pd.isna(val), f"amount 全 NaN 求和后应为 NaN，得到 {val!r}（若为 0 则上层会误读为零成交）"


def test_amount_partial_nan_sums_rest():
    """部分 NaN 时，sum(min_count=1) 语义：有值则求和（pandas 默认 skipna）。"""
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    cal = make_calendar(days)
    amount = [float("nan")] + [1e6, 2e6, 3e6, 4e6]
    df = make_daily(days, amount=amount)
    out = aggregate(df, "weekly", calendar=cal, asof=days[-1])
    assert out.iloc[0]["amount"] == pytest.approx(1e7)


def test_volume_sums():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    cal = make_calendar(days)
    df = make_daily(days)
    out = aggregate(df, "weekly", calendar=cal, asof=days[-1])
    assert out.iloc[0]["volume"] == pytest.approx(sum(1000 + i for i in range(5)))


# ---------------------------------------------------------------------------
# 5. warm-up
# ---------------------------------------------------------------------------
def test_warmup_insufficient_raises():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))  # 只有 5 天
    cal = make_calendar(days)
    df = make_daily(days)
    with pytest.raises(AggregationError, match="warm-up"):
        aggregate(df, "monthly", calendar=cal, warmup_days=30, asof=days[-1])


def test_warmup_sufficient_ok():
    days = weekdays(_dt.date(2026, 7, 1), _dt.date(2026, 9, 30))
    cal = make_calendar(days)
    df = make_daily(days)
    out = aggregate(df, "monthly", calendar=cal, warmup_days=21, asof=days[-1])
    assert len(out) == 3


# ---------------------------------------------------------------------------
# 6. 确定性（重跑逐位一致）
# ---------------------------------------------------------------------------
def test_rerun_bitwise_identical():
    days = weekdays(_dt.date(2026, 6, 1), _dt.date(2026, 9, 30))
    cal = make_calendar(days)
    df = make_daily(days)
    a = aggregate(df, "weekly", calendar=cal, asof=days[-1])
    b = aggregate(df, "weekly", calendar=cal, asof=days[-1])
    pd.testing.assert_frame_equal(a, b)


def test_rerun_with_shuffled_input():
    """输入顺序不应影响结果（按 code/date 排序后再聚合）。"""
    days = weekdays(_dt.date(2026, 6, 1), _dt.date(2026, 9, 30))
    cal = make_calendar(days)
    df = make_daily(days)
    a = aggregate(df, "weekly", calendar=cal, asof=days[-1])
    b = aggregate(df.sample(frac=1.0, random_state=42).reset_index(drop=True),
                  "weekly", calendar=cal, asof=days[-1])
    pd.testing.assert_frame_equal(a, b)


# ---------------------------------------------------------------------------
# 7. 参数化列口径（不硬编码）
# ---------------------------------------------------------------------------
def test_cols_parameterized():
    """§13 教训：硬编码列名 -> 双实现漂移。只聚合指定列。"""
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    cal = make_calendar(days)
    df = make_daily(days)
    out = aggregate(df, "weekly", calendar=cal, cols=["close"], asof=days[-1])
    assert "close" in out.columns
    assert "open" not in out.columns, "未指定的列不应出现在输出中"


def test_missing_close_raises():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    cal = make_calendar(days)
    df = make_daily(days).drop(columns=["close"])
    with pytest.raises(AggregationError, match="close"):
        aggregate(df, "weekly", calendar=cal, asof=days[-1])


# ---------------------------------------------------------------------------
# 8. 多标的
# ---------------------------------------------------------------------------
def test_multiple_codes():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    cal = make_calendar(days)
    a = make_daily(days, code="sh000001")
    b = make_daily(days, code="sz399001", close=[200.0 + i for i in range(len(days))])
    out = aggregate(pd.concat([a, b], ignore_index=True), "weekly",
                    calendar=cal, asof=days[-1])
    assert set(out["code"]) == {"sh000001", "sz399001"}
    assert len(out) == 2


# ---------------------------------------------------------------------------
# 9. 派生一致性断言
# ---------------------------------------------------------------------------
def test_derived_consistency_pass():
    assert_derived_consistent({"derived_from": {"sha256": "abc", "freq": "daily"}}, "abc")


def test_derived_consistency_stale_raises():
    with pytest.raises(AssertionError, match="staleness"):
        assert_derived_consistent({"derived_from": {"sha256": "old"}}, "new")


def test_derived_consistency_missing_field_raises():
    with pytest.raises(AssertionError, match="derived_from"):
        assert_derived_consistent({}, "new")


# ---------------------------------------------------------------------------
# 10. ETF 域 H-1 等价验证：closed_only 剔除未完成周
# ---------------------------------------------------------------------------
def test_closed_only_removes_partial_week():
    """模拟 ETF 域 §31 H-1：未完成 ISO 周必须能被剔除。

    不剔除的后果：97.7% 周中日期信号不同。
    """
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = make_calendar(days)
    df = make_daily(days)
    asof = _dt.date(2026, 9, 16)  # 周三，本周未走完
    out = aggregate(df, "weekly", calendar=cal, asof=asof)

    complete = out[~out[PARTIAL_FLAG].astype(bool)]
    partial = out[out[PARTIAL_FLAG].astype(bool)]
    assert len(partial) == 1
    assert partial.iloc[0]["period_key"] == iso_week_key(asof)
    # closed_only 语义：只保留 complete
    assert len(complete) == 1
    assert complete.iloc[0]["period_key"] == "2026-W37"


def test_aggregator_version_recorded():
    assert AGGREGATOR_VERSION.startswith("common/aggregate.py@")
