"""门禁层 —— 所有数据 workflow 与策略链路共用的硬性检查。

    覆盖率（coverage）    §0.4/§9.9   <95% 黄 / <80% 红且不落盘
    交易日（trading_day） §2.6 H2/H3  cron 首步闸门 + 指数日K二次校验
    盘中（intraday）      §2.6 H4     半截 bar 清洗
    整点（verify）        §2.4        manifest / 契约 / 派生一致性

设计原则：**任何丢弃数据的路径都必须留痕**（§9.9 第 6 条）。
门禁不静默 —— 要么放行并记录，要么红并中止。
"""

from .coverage import (  # noqa: F401
    GREEN,
    RED,
    WARN_THRESHOLD,
    FAIL_THRESHOLD,
    CoverageGateError,
    CoverageResult,
    check_coverage,
    check_stock_coverage,
)
from .intraday import IntradayGuard, clean_intraday_bars, is_bar_closed  # noqa: F401
from .trading_day import (  # noqa: F401
    TradingDayGate,
    TradingDayProbe,
    probe_trading_day_via_index,
)

__all__ = [
    "GREEN", "YELLOW", "RED", "WARN_THRESHOLD", "FAIL_THRESHOLD",
    "CoverageGateError", "CoverageResult", "check_coverage", "check_stock_coverage",
    "IntradayGuard", "clean_intraday_bars", "is_bar_closed",
    "TradingDayGate", "TradingDayProbe", "probe_trading_day_via_index",
]

YELLOW = "yellow"
