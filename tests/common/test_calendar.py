"""交易日历单测 —— 覆盖方案 §2.6 的 H1/H3/H4/H5/H6 与 §2.7 的 is_partial 前置依赖。"""

from __future__ import annotations

import datetime as _dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from common.calendar import (  # noqa: E402
    CalendarExpired,
    TradeCalendar,
    calendar_health,
    data_lag_trading_days,
    iso_week_bounds,
    iso_week_key,
    load_calendar_from_csv,
    month_key,
    set_default,
)


def weekdays(start, end, skip=()):
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += _dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# H1：基础查询
# ---------------------------------------------------------------------------
def test_basic_queries():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = TradeCalendar(days, source="test")
    assert len(cal) == 10
    assert cal.min_date == _dt.date(2026, 9, 7)
    assert cal.max_date == _dt.date(2026, 9, 18)
    assert cal.is_trading_day("2026-09-07")
    assert not cal.is_trading_day("2026-09-12")  # 周六
    assert _dt.date(2026, 9, 7) in cal


def test_last_next_trading_day():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = TradeCalendar(days, source="test")
    assert cal.last_trading_day("2026-09-12") == _dt.date(2026, 9, 11)  # 周六 -> 周五
    assert cal.next_trading_day("2026-09-12") == _dt.date(2026, 9, 14)  # 周六 -> 下周一
    assert cal.last_trading_day("2026-09-07") == _dt.date(2026, 9, 7)


def test_shift():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = TradeCalendar(days, source="test")
    assert cal.shift("2026-09-11", 1) == _dt.date(2026, 9, 14)   # 周五 -> 下周一
    assert cal.shift("2026-09-14", -1) == _dt.date(2026, 9, 11)  # 周一 -> 上周五
    assert cal.shift("2026-09-11", 5) == _dt.date(2026, 9, 18)


def test_range():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = TradeCalendar(days, source="test")
    assert len(cal.range("2026-09-07", "2026-09-11")) == 5
    assert len(cal.range("2026-09-07", "2026-09-18")) == 10


# ---------------------------------------------------------------------------
# H2：cron 首步闸门语义
# ---------------------------------------------------------------------------
def test_holiday_detected():
    """国庆等非周末长假必须能被识别（否则幻影入库连写 5 天）。"""
    nat = [_dt.date(2026, 10, 1), _dt.date(2026, 10, 2), _dt.date(2026, 10, 5),
           _dt.date(2026, 10, 6), _dt.date(2026, 10, 7)]
    days = [d for d in weekdays(_dt.date(2026, 9, 28), _dt.date(2026, 10, 9))
            if d not in nat]
    cal = TradeCalendar(days, holidays={d: "国庆节" for d in nat}, source="test")
    assert not cal.is_trading_day("2026-10-01")
    assert not cal.is_trading_day("2026-10-05")  # 周一但是长假
    assert cal.holiday_name("2026-10-01") == "国庆节"
    assert cal.last_trading_day("2026-10-05") == _dt.date(2026, 9, 30)
    assert cal.next_trading_day("2026-10-05") == _dt.date(2026, 10, 8)


# ---------------------------------------------------------------------------
# H4：半截 bar 清洗
# ---------------------------------------------------------------------------
def test_has_closed_intraday():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    cal = TradeCalendar(days, source="test")
    # 当天 14:00（盘中）-> 未收盘
    t = _dt.datetime(2026, 9, 11, 14, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    assert not cal.has_closed("2026-09-11", now=t)
    # 15:00 -> 已收盘
    t2 = _dt.datetime(2026, 9, 11, 15, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    assert cal.has_closed("2026-09-11", now=t2)
    # 过去的日期恒已收盘
    assert cal.has_closed("2026-09-07", now=t)


def test_half_day_close_time():
    d = _dt.date(2026, 9, 11)
    cal = TradeCalendar([d], half_days=[d], source="test")
    t = _dt.datetime(2026, 9, 11, 11, 30, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    assert cal.has_closed(d, now=t)
    t2 = _dt.datetime(2026, 9, 11, 11, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    assert not cal.has_closed(d, now=t2)


# ---------------------------------------------------------------------------
# H5：落后按交易日算
# ---------------------------------------------------------------------------
def test_data_lag_trading_days():
    # 交易日历：9/7(一) ~ 9/18(五)
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = TradeCalendar(days, source="test")
    set_default(cal)
    # 数据到 9/9（周三），今天 9/11（周五）已收盘
    t_fri = _dt.datetime(2026, 9, 11, 16, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    lag = data_lag_trading_days("2026-09-09", today=t_fri.date())
    assert lag == 2, f"9/9 -> 9/11 应落后 2 个交易日，得到 {lag}"


def test_lag_zero_when_up_to_date():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = TradeCalendar(days, source="test")
    set_default(cal)
    assert data_lag_trading_days("2026-09-18", today=_dt.date(2026, 9, 18)) == 0


def test_lag_holiday_weekend_not_penalized():
    """周末不应被算作落后（H5 的核心：不按自然日算）。"""
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = TradeCalendar(days, source="test")
    set_default(cal)
    # 数据到 9/11（周五），今天 9/13（周日）
    assert data_lag_trading_days("2026-09-11", today=_dt.date(2026, 9, 13)) == 0


# ---------------------------------------------------------------------------
# H6：日历健康度
# ---------------------------------------------------------------------------
def test_calendar_health_ok():
    today = _dt.date(2026, 9, 11)
    days = weekdays(_dt.date(2023, 1, 2), _dt.date(2027, 12, 31))
    cal = TradeCalendar(days, source="test")
    level, msg = calendar_health(cal, today=today, warn_days=90)
    assert level == "ok", msg


def test_calendar_health_error_when_expired():
    today = _dt.date(2026, 9, 11)
    days = weekdays(_dt.date(2023, 1, 2), _dt.date(2026, 1, 30))
    cal = TradeCalendar(days, source="test")
    level, msg = calendar_health(cal, today=today)
    assert level == "error"
    assert "误判为休市" in msg, msg


def test_calendar_health_warning_when_short():
    today = _dt.date(2026, 9, 11)
    days = weekdays(_dt.date(2023, 1, 2), _dt.date(2026, 10, 30))
    cal = TradeCalendar(days, source="test")
    level, msg = calendar_health(cal, today=today, warn_days=90)
    assert level == "warning", msg


# ---------------------------------------------------------------------------
# H3：探测失败放行的语义（宁可漏跑）
# ---------------------------------------------------------------------------
def test_future_date_not_trading_day():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = TradeCalendar(days, source="test")
    assert not cal.is_trading_day("2026-12-31")


# ---------------------------------------------------------------------------
# is_partial 的日历侧语义（与聚合器测试互补）
# ---------------------------------------------------------------------------
def test_period_bounds():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 18))
    cal = TradeCalendar(days, source="test")
    assert cal.period_bounds("2026-W37", "weekly") == (
        _dt.date(2026, 9, 7), _dt.date(2026, 9, 13))
    assert cal.period_bounds("2026-09", "monthly") == (
        _dt.date(2026, 9, 1), _dt.date(2026, 9, 30))
    assert cal.period_bounds("2026-02", "monthly") == (
        _dt.date(2026, 2, 1), _dt.date(2026, 2, 28))


def test_is_partial_period_requires_valid_freq():
    cal = TradeCalendar(weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11)), source="t")
    with pytest.raises(ValueError):
        cal.is_partial_period("2026-W37", "daily", "2026-09-11")


# ---------------------------------------------------------------------------
# CSV 加载（ETF 域现成件提升）
# ---------------------------------------------------------------------------
def test_load_from_csv_days_only(tmp_path):
    p = tmp_path / "cal.csv"
    p.write_text("date\n2026-09-07\n2026-09-08\n2026-09-09\n", encoding="utf-8")
    cal = load_calendar_from_csv(str(p))
    assert len(cal) == 3
    assert cal.is_trading_day("2026-09-08")


def test_load_from_csv_with_flag_and_holiday(tmp_path):
    p = tmp_path / "cal.csv"
    p.write_text(
        "date,is_trading_day,holiday_name\n"
        "2026-10-01,0,国庆节\n"
        "2026-10-08,1,\n"
        "2026-10-09,1,\n",
        encoding="utf-8",
    )
    cal = load_calendar_from_csv(str(p))
    assert len(cal) == 2
    assert not cal.is_trading_day("2026-10-01")
    assert cal.holiday_name("2026-10-01") == "国庆节"


def test_load_from_csv_chinese_header(tmp_path):
    p = tmp_path / "cal.csv"
    p.write_text("日期,交易日\n2026-09-07,1\n2026-09-08,1\n", encoding="utf-8")
    cal = load_calendar_from_csv(str(p))
    assert len(cal) == 2


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def test_to_json_roundtrip():
    days = weekdays(_dt.date(2026, 9, 7), _dt.date(2026, 9, 11))
    cal = TradeCalendar(days, holidays={_dt.date(2026, 9, 10): "测试节"}, source="t")
    j = cal.to_json()
    cal2 = TradeCalendar.from_json(j, days)
    assert cal2.is_trading_day("2026-09-07")
    assert cal2.holiday_name("2026-09-10") == "测试节"
    assert len(cal2) == len(cal)


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------
def test_empty_calendar_raises():
    with pytest.raises(ValueError, match="empty"):
        TradeCalendar([])


def test_iso_week_key_and_bounds_consistency():
    for d in weekdays(_dt.date(2026, 1, 1), _dt.date(2026, 12, 31)):
        key = iso_week_key(d)
        s, e = iso_week_bounds(key)
        assert s <= d <= e, f"{d} not in {key} bounds {s}..{e}"
        assert s.weekday() == 0
