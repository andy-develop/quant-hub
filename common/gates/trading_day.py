"""H2 · cron 首步交易日闸门。

    is_trading_day(today) == False  ->  立即 exit 0
    不抓、不写、不算、不建页面、不 commit

出处：量化域 P0 #18 —— 只看"周几+15:05"会让国庆等非周末长假 cron 照常触发，
把上一交易日行情以假期日期**幻影入库连写 5 天**，且 `verify_store` 查不出来，
信号/回测多出虚假交易日。

H3 · 日历之外二次校验：用指数日K探测 target_day 是否真实存在。
**未来日期接口返回空数组时，按"探测失败"放行** —— 宁可漏跑，不可写脏。
（日历可能因未刷新而缺当年数据，探测是第二道防线。）
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys

from ..calendar import BEIJING, TradeCalendar, load_calendar

__all__ = [
    "is_trading_day_or_exit",
    "TradingDayGate",
    "probe_trading_day_via_index",
    "TradingDayProbe",
]


class TradingDayGate:
    """交易日闸门。

    用法（所有数据 workflow 第一步）：

        gate = TradingDayGate()
        if not gate.should_run():
            sys.exit(0)
    """

    def __init__(self, calendar: TradeCalendar | None = None,
                 root: str | None = None, *, allow_missing_calendar: bool = False):
        self.root = root or os.environ.get("QH_DATA_ROOT", "data")
        try:
            self.cal = calendar or load_calendar(root=self.root)
        except FileNotFoundError:
            if not allow_missing_calendar:
                raise
            self.cal = None

    def today(self) -> _dt.date:
        return _dt.datetime.now(BEIJING).date()

    def should_run(self, day: _dt.date | str | None = None) -> bool:
        if self.cal is None:
            # 无日历时不放行（宁缺勿错）；allow_missing_calendar 只用于过渡期
            return False
        d = day or self.today()
        return self.cal.is_trading_day(d)

    def reason(self, day: _dt.date | str | None = None) -> str:
        d = day or self.today()
        if self.cal is None:
            return "交易日历缺失 —— H2 无法判定，按休市处理（宁缺勿错）"
        if self.cal.is_trading_day(d):
            return f"{d} 是交易日"
        nm = self.cal.holiday_name(d) or ("周末" if d.weekday() >= 5 else "非交易日")
        return f"{d} 非交易日（{nm}）—— exit 0，不抓不写不算不发布"


class TradingDayProbe:
    """H3：用指数日K二次校验 target_day 是否真实存在。

    实测结论（quant-lab 已验证）：
      - 过去节假日当天查询 ifzq，bars 截至上一交易日且不含当天
      - **未来日期返回空数组 -> 按探测失败放行**（避免网络问题漏掉真实交易日）
    """

    def __init__(self, probe_fn=None, symbol: str = "sh000001"):
        self.probe_fn = probe_fn or _default_probe
        self.symbol = symbol

    def is_real_trading_day(self, target: _dt.date) -> tuple[bool, str]:
        try:
            bars = self.probe_fn(self.symbol, target)
        except Exception as e:  # 网络异常 -> 放行（宁可漏跑，不可写脏）
            return True, f"探测失败({type(e).__name__}), 按交易日继续"
        if not bars:
            return True, "接口返回空数组（未来日期或网络问题）, 按探测失败放行"
        has_today = any(_bar_date(b) == target for b in bars)
        if has_today:
            return True, f"指数日K存在 {target} bar"
        return False, f"{target} 非交易日（指数日K无当日 bar）"


def probe_trading_day_via_index(target: _dt.date | str, *, probe_fn=None,
                                symbol: str = "sh000001") -> tuple[bool, str]:
    t = target if isinstance(target, _dt.date) else _dt.date.fromisoformat(str(target)[:10])
    return TradingDayProbe(probe_fn=probe_fn, symbol=symbol).is_real_trading_day(t)


def _bar_date(bar) -> _dt.date | None:
    try:
        if isinstance(bar, (list, tuple)) and bar:
            return _dt.date.fromisoformat(str(bar[0])[:10])
        if isinstance(bar, dict) and "date" in bar:
            return _dt.date.fromisoformat(str(bar["date"])[:10])
    except Exception:
        return None
    return None


def _default_probe(symbol: str, target: _dt.date):
    """默认探测：腾讯 web.ifzq 日K（与个股回补同源）。"""
    import requests

    start = (target - _dt.timedelta(days=20)).strftime("%Y-%m-%d")
    r = requests.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{symbol},day,{start},{target:%Y-%m-%d},20,"},
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    d = r.json()["data"][symbol]
    return [b for b in (d.get("day") or []) if isinstance(b, list) and len(b) >= 6]


def is_trading_day_or_exit(day: _dt.date | str | None = None,
                           root: str | None = None) -> None:
    gate = TradingDayGate(root=root)
    if not gate.should_run(day):
        print(f"[H2] {gate.reason(day)}", flush=True)
        sys.exit(0)
    print(f"[H2] {gate.reason(day)}", flush=True)


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="H2 交易日闸门")
    ap.add_argument("--day", default=None)
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    gate = TradingDayGate(root=args.data_root)
    ok = gate.should_run(args.day)
    msg = gate.reason(args.day)
    if args.json:
        print(json.dumps({"is_trading_day": ok, "reason": msg}, ensure_ascii=False))
    else:
        print(f"[H2] {msg} -> {'继续' if ok else 'exit 0'}")
    return 0 if ok else 0  # 注意：非交易日返回 0（workflow 正常跳过，不是失败）


if __name__ == "__main__":
    raise SystemExit(main())
