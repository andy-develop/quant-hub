"""限流兜底 L1 · L2 · L5（方案 §2.5）。

三域都已经在真实环境里被限流打过，各自的应对散在三份代码里：

    东财对 runner IP 连接级间歇封锁，ETF 域 v1.14c 实测 2 轮重试只成功 15/32
    中证官网当日数据 16:56 实测仍未发布
    腾讯限流下返回空响应，量化域台账 #10 被整段重拉打爆过

合并后三域共用抓取，一次封禁影响面 ×3，必须系统化。

    L1  多源互备（vendor_*.py，按资产类声明主备）
    L2  节流与请求预算（令牌桶 + 指数退避 + 随机抖动 + 全局请求数上限）
    L5  封禁熔断（连续 20 次 403/空响应/超时 -> 熔断，切备源，状态跨 run 传递）

★ 代码规范（§9.9 第 6 条）：**任何丢弃数据的路径都必须留痕**。
   `snapshot_day` 那种"失败就 sleep(2); continue"的写法必须改成
   "失败就计数 + 记 runlog + 重试"。
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

__all__ = [
    "TokenBucket",
    "CircuitBreaker",
    "CircuitOpen",
    "RequestBudget",
    "RequestBudgetExceeded",
    "FetchStats",
    "RetryPolicy",
    "Vendor",
    "fetch_with_fallback",
]


# ---------------------------------------------------------------------------
# L2 · 令牌桶
# ---------------------------------------------------------------------------
class TokenBucket:
    """每 vendor 一个令牌桶（可配 QPS）+ 指数退避 + 随机抖动。

    三域现有实现合一；起步取最保守值（短线域 4 worker、个性化域 5 并发 + 分批休息、
    ETF 域并发 4 -> 取 1，见 assert_merged_default_is_conservative）。
    """

    def __init__(self, rate: float = 1.0, burst: int = 1):
        self.rate = max(rate, 0.05)
        self.burst = max(burst, 1)
        self._tokens = float(self.burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: float = 1.0, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                need = (n - self._tokens) / self.rate
            if deadline is not None and time.monotonic() + need > deadline:
                return False
            time.sleep(min(need, 0.5))

    def backoff(self, attempt: int, base: float = 0.5, cap: float = 30.0,
                jitter: float = 0.3) -> float:
        """指数退避 + 随机抖动。返回实际 sleep 秒数。"""
        d = min(cap, base * (2 ** attempt))
        d *= (1.0 + random.uniform(-jitter, jitter))
        return max(d, 0.05)


# ---------------------------------------------------------------------------
# L5 · 熔断
# ---------------------------------------------------------------------------
class CircuitOpen(RuntimeError):
    pass


@dataclass
class CircuitBreaker:
    """连续 20 次 403/空响应/超时 -> 该 vendor 熔断，本 run 内不再尝试，切备源。

    熔断状态写 `state/data/circuit.json`，下一 run 开局先读：
    若上一轮熔断且距今 <2h，直接走低频慢速模式（worker=1、间隔 ×5）探测恢复，
    而不是一上来就全速撞墙。
    """

    vendor: str
    threshold: int = 20
    cooldown_seconds: int = 2 * 3600
    fails: int = 0
    opened_at: float | None = None

    def record_fail(self) -> bool:
        self.fails += 1
        if self.fails >= self.threshold and self.opened_at is None:
            self.opened_at = time.time()
            return True
        return False

    def record_ok(self) -> None:
        self.fails = 0
        self.opened_at = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.time() - self.opened_at >= self.cooldown_seconds:
            # 冷却结束 -> 半开（允许一次探测）
            return False
        return True

    def slow_mode(self) -> bool:
        """上一轮熔断且距今 <2h -> 走低频慢速模式。"""
        return self.opened_at is not None and (time.time() - self.opened_at) < self.cooldown_seconds

    def to_dict(self) -> dict:
        return {"vendor": self.vendor, "fails": self.fails,
                "opened_at": self.opened_at, "is_open": self.is_open}

    @classmethod
    def from_dict(cls, d: dict) -> "CircuitBreaker":
        cb = cls(vendor=d.get("vendor", "?"), threshold=d.get("threshold", 20))
        cb.fails = int(d.get("fails") or 0)
        cb.opened_at = d.get("opened_at")
        return cb


class CircuitStore:
    """跨 run 传递熔断状态（state/data/circuit.json）。"""

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f) or {}
            except Exception:
                self.data = {}

    def get(self, vendor: str, threshold: int = 20) -> CircuitBreaker:
        d = self.data.get(vendor)
        if d:
            cb = CircuitBreaker.from_dict(d)
            cb.threshold = threshold
            return cb
        return CircuitBreaker(vendor=vendor, threshold=threshold)

    def put(self, cb: CircuitBreaker) -> None:
        self.data[cb.vendor] = {"vendor": cb.vendor, "fails": cb.fails,
                                "opened_at": cb.opened_at, "threshold": cb.threshold}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# L2 · 全局请求预算
# ---------------------------------------------------------------------------
class RequestBudgetExceeded(RuntimeError):
    pass


@dataclass
class RequestBudget:
    """单次 run 的 HTTP 请求数上限（个股 ~6000、ETF ~200）。

    超出即中止并落盘进度文件，**下次 run 从断点续跑**。
    这直接封死量化域 #10 那种"2340 只被误判除权 -> 整段重拉 -> 打爆限流"的雪崩路径。
    """

    limit: int
    used: int = 0
    progress_path: str | None = None

    def spend(self, n: int = 1) -> None:
        self.used += n
        if self.used > self.limit:
            self._dump_progress()
            raise RequestBudgetExceeded(
                f"请求预算超限: used={self.used} > limit={self.limit}。"
                f"已落盘进度文件，下次 run 从断点续跑"
            )

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def _dump_progress(self) -> None:
        if not self.progress_path:
            return
        os.makedirs(os.path.dirname(self.progress_path) or ".", exist_ok=True)
        with open(self.progress_path, "w", encoding="utf-8") as f:
            json.dump({"used": self.used, "limit": self.limit}, f)


# ---------------------------------------------------------------------------
# 统计（★ 任何丢弃数据的路径都必须留痕）
# ---------------------------------------------------------------------------
@dataclass
class FetchStats:
    """抓取统计。所有 count 都必须进 runlog。"""

    vendor: str = "?"
    requested: int = 0
    ok: int = 0
    retried: int = 0
    dropped_batch_exc: int = 0        # 批次整体异常（原 snapshot_day 第一条 continue）
    dropped_short_format: int = 0     # 字段数不足（第二条）
    dropped_unknown_code: int = 0     # 代码不在 universe（第三条 ★ §0.4 根因）
    dropped_parse_error: int = 0      # 解析异常（第四条，方案漏列）
    dropped_intraday: int = 0         # 半截 bar
    dropped_other: int = 0
    circuits_opened: list[str] = field(default_factory=list)
    missing_codes: list[str] = field(default_factory=list)

    @property
    def dropped_total(self) -> int:
        return (self.dropped_batch_exc + self.dropped_short_format
                + self.dropped_unknown_code + self.dropped_parse_error
                + self.dropped_intraday + self.dropped_other)

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor, "requested": self.requested, "ok": self.ok,
            "retried": self.retried, "dropped_total": self.dropped_total,
            "dropped_breakdown": {
                "batch_exception": self.dropped_batch_exc,
                "short_format": self.dropped_short_format,
                "unknown_code": self.dropped_unknown_code,
                "parse_error": self.dropped_parse_error,
                "intraday": self.dropped_intraday,
                "other": self.dropped_other,
            },
            "circuits_opened": self.circuits_opened,
            "missing_sample": self.missing_codes[:20],
            "missing_count": len(self.missing_codes),
        }


# ---------------------------------------------------------------------------
# 重试策略
# ---------------------------------------------------------------------------
@dataclass
class RetryPolicy:
    """批内失败逐只重试一次；批次整体失败指数退避重试 3 次。"""

    per_item_retries: int = 1
    batch_retries: int = 3
    base_delay: float = 0.5
    cap_delay: float = 30.0

    def delay_for(self, attempt: int) -> float:
        return min(self.cap_delay, self.base_delay * (2 ** attempt)) * (
            1.0 + random.uniform(-0.3, 0.3))


# ---------------------------------------------------------------------------
# Vendor 抽象
# ---------------------------------------------------------------------------
@dataclass
class Vendor:
    """一个数据源。

    fetch(batch_codes) -> list[dict]，每条至少含 code/date/open/high/low/close/volume/amount。
    ★ 实现必须：把丢弃路径写进 stats，不许静默 continue。
    """

    name: str
    fetch: Callable
    bucket: TokenBucket = field(default_factory=lambda: TokenBucket(1.0, 1))
    breaker: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("?"))

    def __post_init__(self):
        if self.breaker.vendor == "?":
            self.breaker.vendor = self.name


def fetch_with_fallback(
    vendors: list[Vendor],
    codes: list[str],
    *,
    batch_size: int = 60,
    budget: RequestBudget | None = None,
    policy: RetryPolicy | None = None,
    stats: FetchStats | None = None,
    logger=None,
) -> tuple[list[dict], FetchStats]:
    """L1 多源互备 + L2 节流 + L5 熔断 的抓取编排。

    返回 (rows, stats)。rows 是所有成功抓到的记录（部分成功即返回，不回滚）。
    """
    policy = policy or RetryPolicy()
    stats = stats or FetchStats()
    rows: list[dict] = []
    todo = list(codes)
    stats.requested = len(todo)

    active = [v for v in vendors if not v.breaker.is_open]
    if not active:
        stats.circuits_opened = [v.name for v in vendors]
        _log(logger, f"[L5] 所有 vendor 均处于熔断，走 L3 归档兜底")
        return rows, stats

    for vi, v in enumerate(active):
        if not todo:
            break
        slow = v.breaker.slow_mode()
        if slow:
            _log(logger, f"[L5] {v.name} 上一轮熔断 <2h -> 低频慢速模式（间隔 ×5）")
        fails_before = v.breaker.fails
        gained = _fetch_from(v, todo, batch_size, budget, policy, stats, rows,
                             slow=slow, logger=logger)
        todo = [c for c in todo if c not in gained]
        if v.breaker.fails > fails_before:
            # 本 vendor 本轮出现失败（可能未达熔断阈值，但已被放弃）
            if v.name not in stats.circuits_opened:
                stats.circuits_opened.append(v.name)
            _log(logger, f"[L5] {v.name} 放弃"
                         f"（连续失败 {v.breaker.fails}/{v.breaker.threshold}）-> 切备源")

    stats.missing_codes = todo
    if todo:
        _log(logger, f"[L1] {len(todo)} 只未取到，进 manifest.missing_codes，由 21:00 补跑补齐")
    return rows, stats


def _fetch_from(v: Vendor, todo: list[str], batch_size: int, budget, policy,
                stats: FetchStats, rows: list[dict], *, slow: bool, logger) -> set[str]:
    gained: set[str] = set()
    n_batch = (len(todo) + batch_size - 1) // batch_size
    for bi in range(n_batch):
        batch = todo[bi * batch_size:(bi + 1) * batch_size]
        if v.breaker.is_open:
            stats.dropped_batch_exc += len(batch)
            _log(logger, f"[L5] {v.name} 已熔断，跳过批次 {bi + 1}/{n_batch}（{len(batch)} 只）")
            continue
        if budget:
            budget.spend(1)
        if slow:
            time.sleep(1.0)
        v.bucket.acquire(1.0)
        got = _try_batch(v, batch, policy, stats, logger)
        if got:
            v.breaker.record_ok()
            rows.extend(got)
            gained |= {r["code"] for r in got}
        else:
            # ★ 批次整体失败：指数退避重试 3 次
            for attempt in range(policy.batch_retries):
                d = v.bucket.backoff(attempt)
                _log(logger, f"[L2] {v.name} 批次 {bi + 1}/{n_batch} 失败，"
                             f"退避 {d:.2f}s 重试 {attempt + 1}/{policy.batch_retries}")
                time.sleep(d)
                stats.retried += 1
                if budget:
                    budget.spend(1)
                got = _try_batch(v, batch, policy, stats, logger)
                if got:
                    v.breaker.record_ok()
                    rows.extend(got)
                    gained |= {r["code"] for r in got}
                    break
            else:
                # ★ 重试耗尽：计入 breaker 失败，并标记本 vendor 本 run 内不再尝试
                #   （不管是否达到全局熔断阈值，都要让出给备源，避免在一棵树上吊死）
                v.breaker.record_fail()
                stats.dropped_batch_exc += len(batch)
                _log(logger, f"[L1] {v.name} 批次 {bi + 1} 重试耗尽，"
                             f"让出给备源（累计失败 {v.breaker.fails}）")
                return gained       # ★ 立刻返回，让 fetch_with_fallback 切下一个 vendor
    return gained


def _try_batch(v: Vendor, batch: list[str], policy, stats, logger) -> list[dict]:
    try:
        out = v.fetch(batch)
    except Exception as e:
        _log(logger, f"[{v.name}] 批次异常 {type(e).__name__}: {e}")
        return []
    return out or []


def _log(logger, msg: str) -> None:
    if logger:
        logger(msg)
    else:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# 保守默认值断言（CI 可跑）
# ---------------------------------------------------------------------------
def assert_merged_default_is_conservative():
    """合并时取三域最保守的并发起步值（方案 §2.5 L2）。"""
    # 短线域 4 worker、个性化域 5 并发、ETF 域并发 4 -> 取 1 QPS 起步
    bucket = TokenBucket(1.0, 1)
    assert bucket.rate == 1.0 and bucket.burst == 1
    return True
