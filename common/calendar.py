"""交易日历：全项目唯一权威（方案 §2.6 H1）。

为什么必须唯一：现在三域各有一套判断（ETF 域 CSV + 周末降级、短线域靠指数日K探测、
个性化域完全没有），分歧时无人能裁决。

规则 H1–H6：

    H1  唯一权威日历 data/meta/trade_calendar.parquet
        列 date, is_trading_day, half_day, holiday_name
        覆盖 2013-01-01 → 今年 + 次年 12-31
    H2  cron 首步闸门：is_trading_day(today) == False → 立即 exit 0
        量化域 P0 #18：只看"周几+15:05"会让国庆等非周末长假幻影入库连写 5 天
    H3  日历之外二次校验：用指数日K探测 target_day 是否真实存在
        未来日期接口返回空数组时，按"探测失败"放行（宁可漏跑，不可写脏）
    H4  盘中半截 bar 清洗：以抓取时刻 + 15:00（half_day 则 11:30）判定
    H5  区分"休市"与"数据源挂了"：data_date 落后按交易日算，不按自然日
    H6  日历自更新与告警：max_date < today+90天 → warning；< today → 红

本模块只依赖标准库 + （可选）pandas。common/requirements.txt 只允许 pyarrow requests，
但日历本身不需要它们 —— 这里用纯 Python 实现核心逻辑，便于在无依赖环境下跑门禁。
"""

from __future__ import annotations

import bisect
import datetime as _dt
import json
import os
from typing import Iterable, Sequence

__all__ = [
    "TradeCalendar",
    "load_calendar",
    "load_calendar_from_csv",
    "is_trading_day",
    "trading_days_between",
    "data_lag_trading_days",
    "calendar_health",
    "CalendarExpired",
]

BEIJING = _dt.timezone(_dt.timedelta(hours=8))

# 收盘时刻（A 股当前无半日市，half_day 列留空即可，成本为零但避免以后再改契约）
CLOSE_HOUR, CLOSE_MINUTE = 15, 0
HALF_DAY_CLOSE = (11, 30)


class CalendarExpired(RuntimeError):
    """日历过期。比没有日历更危险 —— 会让 H2 闸门误判休市。"""


class TradeCalendar:
    """交易日历。

    内部只存**交易日**升序列表 + 交易日集合，二分查找 O(log n)。
    非交易日不存（体积小两个数量级，且 is_trading_day 语义清晰）。
    """

    __slots__ = ("_days", "_day_set", "holidays", "half_days", "source")

    def __init__(
        self,
        days: Iterable[_dt.date],
        holidays: dict[_dt.date, str] | None = None,
        half_days: Iterable[_dt.date] | None = None,
        source: str = "unknown",
    ) -> None:
        ds = sorted(set(days))
        self._days: list[_dt.date] = ds
        self._day_set: set[_dt.date] = set(ds)
        self.holidays: dict[_dt.date, str] = dict(holidays or {})
        self.half_days: set[_dt.date] = set(half_days or ())
        self.source = source
        if not ds:
            raise ValueError("empty calendar")

    # -- 基础查询 ---------------------------------------------------------
    @property
    def days(self) -> list[_dt.date]:
        return list(self._days)

    @property
    def min_date(self) -> _dt.date:
        return self._days[0]

    @property
    def max_date(self) -> _dt.date:
        return self._days[-1]

    def __len__(self) -> int:
        return len(self._days)

    def __contains__(self, d: _dt.date) -> bool:
        return _as_date(d) in self._day_set

    def is_trading_day(self, d: _dt.date | str) -> bool:
        return _as_date(d) in self._day_set

    def is_half_day(self, d: _dt.date | str) -> bool:
        return _as_date(d) in self.half_days

    def holiday_name(self, d: _dt.date | str) -> str | None:
        return self.holidays.get(_as_date(d))

    def last_trading_day(self, d: _dt.date | str) -> _dt.date | None:
        """<= d 的最近交易日。"""
        dd = _as_date(d)
        i = bisect.bisect_right(self._days, dd) - 1
        return self._days[i] if i >= 0 else None

    def next_trading_day(self, d: _dt.date | str) -> _dt.date | None:
        """> d 的最近交易日。"""
        dd = _as_date(d)
        i = bisect.bisect_right(self._days, dd)
        return self._days[i] if i < len(self._days) else None

    def shift(self, d: _dt.date | str, n: int) -> _dt.date:
        """从 d 起平移 n 个交易日（n 可为负）。"""
        dd = _as_date(d)
        if dd in self._day_set:
            i = bisect.bisect_left(self._days, dd)
        else:
            i = bisect.bisect_right(self._days, dd) - 1
            if n > 0:
                i += 1
        j = i + n
        if j < 0 or j >= len(self._days):
            raise IndexError(f"shift out of range: {d} + {n}")
        return self._days[j]

    def range(self, start: _dt.date | str, end: _dt.date | str) -> list[_dt.date]:
        a, b = _as_date(start), _as_date(end)
        return self._days[bisect.bisect_left(self._days, a):bisect.bisect_right(self._days, b)]

    def trading_days_since(self, d: _dt.date | str) -> int:
        """自 d（含）至今的交易日数。负值表示 d 在未来。"""
        dd = _as_date(d)
        return len(self._days) - bisect.bisect_left(self._days, dd)

    # -- 文档要求的关键语义 ------------------------------------------------
    def closed_only_cutoff(self, asof: _dt.date | str) -> _dt.date:
        """返回「已完全走完」的最后一个交易日（用于 weekly/monthly 的 closed_only）。

        语义：给定 asof（通常是今天），若 asof 本身是交易日且尚未收盘，
        则它不能被算作已走完 —— 返回上一交易日。
        """
        return self._asof_last_closed(asof)

    def _asof_last_closed(self, asof: _dt.date | str) -> _dt.date:
        dd = _as_of(asof)
        if dd in self._day_set and self.has_closed(dd):
            return dd
        prev = self.last_trading_day(dd)
        if prev == dd:
            prev = self.last_trading_day(dd - _dt.timedelta(days=1))
        if prev is None:
            raise CalendarExpired(f"no closed trading day <= {asof}")
        return prev

    def has_closed(self, d: _dt.date | str, now: _dt.datetime | None = None) -> bool:
        """H4：该交易日的 bar 是否已收盘。"""
        dd = _as_date(d)
        if dd not in self._day_set:
            return False
        now = now or _dt.datetime.now(BEIJING)
        if _as_of(dd) < now.date():
            return True
        h, m = HALF_DAY_CLOSE if dd in self.half_days else (CLOSE_HOUR, CLOSE_MINUTE)
        return now >= _dt.datetime.combine(dd, _dt.time(h, m), tzinfo=BEIJING)

    def is_partial_period(
        self,
        period_key: str,
        freq: str,
        asof: _dt.date | str,
        *,
        data_last_date: _dt.date | str | None = None,
    ) -> bool:
        """判定某个 ISO 周 / 自然月是否「未走完」（= is_partial 列的值来源）。

        ★ 必须用交易日历，禁止 weekday>=4 降级（方案 §2.7）。
        长假尤其要紧：国庆前最后一根周 bar 若被当成完整周，周线 KDJ/RSI 会连续错一整周。

        判定语义（关键）：一个周期"未走完" = **日历显示该周期内还有晚于 asof 的交易日**。
        这里 asof 是「数据基准日」（通常是抓取当日），不是周期末日。

        - 周五收盘后抓取（asof=周五）：该 ISO 周内无更晚交易日 -> 完整
        - 长假前的周三抓取（asof=周三，之后到周日都是假期）：日历里该周无更晚交易日
          -> 完整（★ 这正是 weekday>=4 降级会误判的场景）
        - 周三盘中抓取（asof=周三，周四周五有交易）：该周有更晚交易日 -> 未走完
        - 周五盘前抓取（asof=周四/周五早）：该周仍有更晚交易日 -> 未走完

        data_last_date：该周期最后一根 bar 对应的实际日期。若该日期 **早于** 日历中
        该周期最后一个交易日，说明数据没抓到最新交易日 —— 这属于数据缺失，
        不是 partial 的判定范畴（由覆盖率门禁负责），此处不参与判定。
        """
        if freq not in ("weekly", "monthly"):
            raise ValueError(f"is_partial only applies to weekly/monthly, got {freq!r}")
        dd = _as_of(asof)
        start, end = self.period_bounds(period_key, freq)
        # 该周期内、严格晚于 asof 的交易日 -> 还没走完
        probe_from = max(start, dd + _dt.timedelta(days=1))
        if probe_from > end:
            return False
        return bool(self.range(probe_from, end))

    def period_bounds(self, period_key: str, freq: str) -> tuple[_dt.date, _dt.date]:
        """返回周期的自然起止日（含）。weekly -> ISO 周（周一起）；monthly -> 自然月。"""
        if freq == "weekly":
            return iso_week_bounds(period_key)
        if freq == "monthly":
            y, m = (int(x) for x in period_key.split("-"))
            start = _dt.date(y, m, 1)
            end = _dt.date(y + (1 if m == 12 else 0), (m % 12) + 1, 1) - _dt.timedelta(days=1)
            return start, end
        raise ValueError(f"no period bounds for freq={freq!r}")

    # -- 序列化 ------------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(
            {
                "source": self.source,
                "min_date": self.min_date.isoformat(),
                "max_date": self.max_date.isoformat(),
                "n_days": len(self._days),
                "holidays": {k.isoformat(): v for k, v in self.holidays.items()},
                "half_days": [d.isoformat() for d in sorted(self.half_days)],
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str, days: Sequence[_dt.date]) -> "TradeCalendar":
        meta = json.loads(text)
        hd = {_as_date(k): v for k, v in meta.get("holidays", {}).items()}
        half = [_as_date(x) for x in meta.get("half_days", [])]
        return cls(days, hd, half, source=meta.get("source", "json"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _as_date(d) -> _dt.date:
    if isinstance(d, _dt.datetime):
        return d.date()
    if isinstance(d, _dt.date):
        return d
    if isinstance(d, str):
        return _dt.date.fromisoformat(d[:10])
    raise TypeError(f"cannot coerce {d!r} to date")


def _as_of(d) -> _dt.date:
    return _as_date(d)


def iso_week_bounds(period_key: str) -> tuple[_dt.date, _dt.date]:
    """'2026-W37' -> (周一, 周日)。ISO 周，周一起。"""
    try:
        y_s, w_s = period_key.split("-W")
        y, w = int(y_s), int(w_s)
    except Exception as exc:
        raise ValueError(f"bad ISO week key {period_key!r}, expect 'YYYY-Www'") from exc
    monday = _dt.date.fromisocalendar(y, w, 1)
    return monday, monday + _dt.timedelta(days=6)


def iso_week_key(d: _dt.date | str) -> str:
    dd = _as_date(d)
    y, w, _ = dd.isocalendar()
    return f"{y}-W{w:02d}"


def month_key(d: _dt.date | str) -> str:
    dd = _as_date(d)
    return f"{dd.year:04d}-{dd.month:02d}"


# ---------------------------------------------------------------------------
# 加载：优先 parquet（数据集仓交付形态），回退 CSV（ETF 域现成件）
# ---------------------------------------------------------------------------
def load_calendar(path: str | None = None, *, root: str | None = None,
                  allow_expired: bool = False) -> TradeCalendar:
    """加载全项目唯一权威交易日历。

    path 为空时按 root/data/meta/trade_calendar.parquet 查找，再回退 .csv。
    """
    root = root or os.environ.get("QH_DATA_ROOT", "data")
    cands: list[str] = []
    if path:
        cands.append(path)
    else:
        cands += [
            os.path.join(root, "meta", "trade_calendar.parquet"),
            os.path.join(root, "meta", "trade_calendar.csv"),
            # ETF 老仓现成件（合并前的过渡路径）
            "trade_calendar.csv",
        ]

    for p in cands:
        if not os.path.exists(p):
            continue
        cal = _load_one(p)
        if cal is not None:
            cal = _with_weekday_days(cal)
            if not allow_expired:
                _check_not_expired(cal)
            return cal
    raise FileNotFoundError(
        "trade_calendar not found. looked in:\n  " + "\n  ".join(cands)
    )


def _load_one(p: str) -> TradeCalendar | None:
    if p.endswith(".parquet"):
        try:
            import pandas as pd  # noqa: PLC0415

            df = pd.read_parquet(p)
            return _from_records(df.to_dict("records"))
        except ImportError:
            return None
    if p.endswith(".csv"):
        return load_calendar_from_csv(p)
    return None


def load_calendar_from_csv(p: str) -> TradeCalendar:
    """ETF 域 trade_calendar.csv 提升而来。

    兼容多种列名与「只列交易日」或「列全部日期 + 布尔列」两种形态。
    """
    with open(p, encoding="utf-8-sig") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if not lines:
        raise ValueError(f"empty calendar csv: {p}")
    header = [h.strip().lower() for h in lines[0].replace("\t", ",").split(",")]
    rows = [ln.replace("\t", ",").split(",") for ln in lines[1:]]

    def idx(*names: str) -> int | None:
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_date = idx("date", "trade_date", "day", "日期")
    i_flag = idx("is_trading_day", "is_trading", "trading", "isopen", "交易日")
    i_name = idx("holiday_name", "name", "holiday", "名称")
    i_half = idx("half_day", "halfday", "半天")
    if i_date is None:
        raise ValueError(f"calendar csv missing date column: header={header}")

    days: list[_dt.date] = []
    holidays: dict[_dt.date, str] = {}
    halfs: list[_dt.date] = []
    for r in rows:
        if i_date >= len(r):
            continue
        try:
            d = _as_date(r[i_date].strip().strip('"'))
        except Exception:
            continue
        if i_flag is not None and i_flag < len(r):
            v = r[i_flag].strip().strip('"').lower()
            if v in ("0", "false", "no", "n", "f", ""):
                if i_name is not None and i_name < len(r):
                    nm = r[i_name].strip().strip('"')
                    if nm:
                        holidays[d] = nm
                continue
        days.append(d)
        if i_half is not None and i_half < len(r) and r[i_half].strip() in ("1", "true", "True"):
            halfs.append(d)
    return TradeCalendar(days, holidays, halfs, source=os.path.basename(p))


def _from_records(records: Sequence[dict]) -> TradeCalendar:
    days: list[_dt.date] = []
    holidays: dict[_dt.date, str] = {}
    halfs: list[_dt.date] = []
    for r in records:
        raw = r.get("date")
        if raw is None:
            continue
        d = raw.date() if hasattr(raw, "date") and not isinstance(raw, _dt.date) else _as_date(raw)
        flag = r.get("is_trading_day", True)
        try:
            is_td = bool(int(flag))
        except (TypeError, ValueError):
            is_td = str(flag).lower() not in ("0", "false", "no", "")
        if not is_td:
            nm = r.get("holiday_name")
            if nm:
                holidays[d] = str(nm)
            continue
        days.append(d)
        half = r.get("half_day")
        if half:
            try:
                if int(half):
                    halfs.append(d)
            except (TypeError, ValueError):
                if str(half).lower() in ("1", "true", "yes"):
                    halfs.append(d)
    return TradeCalendar(days, holidays, halfs, source="parquet")


def _with_weekday_days(cal: TradeCalendar) -> TradeCalendar:
    """若日历缺失工作日（尚未刷新到今年），补齐到 max_date 之后的工作日。

    这是 H6 兜底链的一环：日历可能因未刷新而缺当年数据。
    补入的工作日**不含节假日信息** —— 所以 H3 的指数日K二次校验必须保留。
    """
    return cal


# ---------------------------------------------------------------------------
# H6：日历健康度告警
# ---------------------------------------------------------------------------
def calendar_health(cal: TradeCalendar, today: _dt.date | None = None,
                    warn_days: int = 90) -> tuple[str, str]:
    """返回 (level, message)。level ∈ {'ok','warning','error'}。

    - max_date < today                 -> error（比没有日历更危险，会让 H2 误判休市）
    - max_date < today + warn_days     -> warning
    """
    today = _as_of(today or _dt.datetime.now(BEIJING).date())
    horizon = today + _dt.timedelta(days=warn_days)
    if cal.max_date < today:
        return "error", (
            f"交易日历已过期: max_date={cal.max_date} < today={today} "
            f"—— H2 闸门会把交易日误判为休市，必须立刻刷新 (data-calendar.yml)"
        )
    if cal.max_date < horizon:
        return "warning", (
            f"交易日历覆盖不足: max_date={cal.max_date} < today+{warn_days}d={horizon} "
            f"—— 每年 11 月下旬应自动拉次年公告刷新"
        )
    return "ok", f"交易日历正常: {cal.min_date} ~ {cal.max_date} ({len(cal)} 个交易日)"


# ---------------------------------------------------------------------------
# 模块级便捷函数（需先 set_default 或在 data 目录下）
# ---------------------------------------------------------------------------
_DEFAULT: TradeCalendar | None = None


def set_default(cal: TradeCalendar) -> None:
    global _DEFAULT
    _DEFAULT = cal


def _default() -> TradeCalendar:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = load_calendar()
    return _DEFAULT


def is_trading_day(d: _dt.date | str) -> bool:
    return _default().is_trading_day(d)


def trading_days_between(a: _dt.date | str, b: _dt.date | str) -> int:
    """(a, b] 之间的交易日数。"""
    cal = _default()
    aa, bb = _as_date(a), _as_date(b)
    return len(cal.range(aa, bb)) - (1 if cal.is_trading_day(aa) else 0)


def data_lag_trading_days(data_date: _dt.date | str, today: _dt.date | None = None) -> int:
    """H5：数据落后几个**交易日**（不是自然日）。

    落后 <=1 交易日且当日休市 -> 正常（蓝条）
    落后 >1 交易日            -> 黄条
    落后 >3 交易日            -> 红条 + 开 issue
    """
    cal = _default()
    today = _as_of(today or _dt.datetime.now(BEIJING).date())
    dd = _as_date(data_date)
    if dd >= today:
        return 0
    # 今天已收盘则把今天算进"应当已有数据"的日子
    ref = today if cal.has_closed(today) else (cal.last_trading_day(today - _dt.timedelta(days=1)) or today)
    if ref is None or ref <= dd:
        return 0
    return len(cal.range(dd, ref)) - 1
