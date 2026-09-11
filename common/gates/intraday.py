"""H4 · 盘中半截 bar 清洗。

以抓取时刻 + 15:00（`half_day` 则 11:30）判定该 bar 是否已收盘，未收盘剔除。

出处：ETF 域 v1.1 审计整改项 —— 手动 `workflow_dispatch` 在盘中点一下就会抓到
半截 bar，`data_date` 会显示成今天但收盘价是错的。

这是最容易被低估的一类脏数据：页面看起来"更新了"，数字却是错的。
"""

from __future__ import annotations

import datetime as _dt
import os

from ..calendar import BEIJING, CLOSE_HOUR, CLOSE_MINUTE, HALF_DAY_CLOSE, TradeCalendar

__all__ = ["is_bar_closed", "clean_intraday_bars", "IntradayGuard"]


def is_bar_closed(cal: TradeCalendar, day: _dt.date | str,
                  now: _dt.datetime | None = None) -> bool:
    return cal.has_closed(day, now=now)


class IntradayGuard:
    """盘中半截 bar 清洗器。

    用法（抓取落盘前）：

        g = IntradayGuard(cal)
        df = g.clean(df, day_col="date")
        if g.dropped:
            runlog.warn(f"剔除 {g.dropped} 行未收盘 bar")
    """

    def __init__(self, calendar: TradeCalendar, now: _dt.datetime | None = None):
        self.cal = calendar
        self.now = now or _dt.datetime.now(BEIJING)
        self.dropped = 0
        self.kept = 0
        self.reason = ""

    def is_closed(self, day) -> bool:
        d = day if isinstance(day, _dt.date) else _dt.date.fromisoformat(str(day)[:10])
        return self.cal.has_closed(d, now=self.now)

    def clean(self, df, day_col: str = "date", *, pd=None):
        """剔除未收盘当日的行。返回清洗后的帧（复制）。"""
        if pd is None:
            import pandas as pd  # noqa: PLC0415

        if df is None or len(df) == 0:
            return df
        if day_col not in df.columns:
            raise KeyError(f"day column {day_col!r} not in frame: {list(df.columns)}")

        col = pd.to_datetime(df[day_col])
        dates = col.dt.date
        today = self.now.date()

        # 只有"今天"这一行可能未收盘；历史日期恒已收盘
        mask_today = dates == today
        if not mask_today.any():
            self.kept = len(df)
            return df

        closed = self.is_closed(today)
        if closed:
            self.kept = len(df)
            self.reason = f"{today} 已收盘（{self._close_desc(today)}）"
            return df

        out = df[~mask_today].copy()
        self.dropped = int(mask_today.sum())
        self.kept = len(out)
        self.reason = (
            f"盘中抓取：{today} 尚未收盘（收盘时刻 {self._close_desc(today)}，"
            f"当前 {self.now:%H:%M}），剔除 {self.dropped} 行半截 bar"
        )
        return out

    def _close_desc(self, d: _dt.date) -> str:
        h, m = HALF_DAY_CLOSE if self.cal.is_half_day(d) else (CLOSE_HOUR, CLOSE_MINUTE)
        return f"{h:02d}:{m:02d}"


def clean_intraday_bars(df, calendar: TradeCalendar, *, day_col: str = "date",
                        now: _dt.datetime | None = None, pd=None):
    g = IntradayGuard(calendar, now=now)
    out = g.clean(df, day_col=day_col, pd=pd)
    return out, g
