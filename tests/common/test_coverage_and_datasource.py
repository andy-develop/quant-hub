"""§0.4 沪市覆盖故障的修复验证 —— 用真实响应样本回归。

这是合并工程里最重要的一组测试：它证明"连续 4 个交易日丢失整个沪市"
的故障在新实现下不会再发生。

旧实现（有 bug）：
    sym_full = ("sh" if line.startswith("v_sh") else "sz") + sym
    if sym_full not in sym_map:
        continue          # ★ 静默丢弃，不计数

新实现：
    mkt, num, simplified = _market_of_line(line)
    # 显式识别 v_s_ 前缀，不做二值猜测
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from common.datasource import (  # noqa: E402
    CircuitBreaker,
    CircuitStore,
    FetchStats,
    RequestBudget,
    RequestBudgetExceeded,
    RetryPolicy,
    TokenBucket,
    Vendor,
    fetch_with_fallback,
)
from common.datasource.vendor_tencent import (  # noqa: E402
    SNAPSHOT_MIN_FIELDS,
    _market_of_line,
    parse_qt_batch_response,
)


# ---------------------------------------------------------------------------
# 构造真实形态的腾讯响应
# ---------------------------------------------------------------------------
def _full_line(sym: str, close: float = 10.5, prev: float = 10.0,
               opn: float = 10.1, high: float = 10.8, low: float = 9.9,
               vol: float = 123456, amt_wan: float = 1300.5) -> str:
    """构造完整格式响应行（≥40 字段）。

    parts 索引（腾讯实际约定）：
      1=名称 2=代码 3=现价 4=昨收 5=今开 6=成交量(手) 33=最高 34=最低 37=成交额(万元)
    """
    p = [""] * 50
    mkt = sym[:2]
    num = sym[2:]
    p[0] = f"v_{sym}"
    p[1] = "测试股"
    p[2] = num
    p[3] = f"{close}"
    p[4] = f"{prev}"
    p[5] = f"{opn}"
    p[6] = f"{vol}"
    p[33] = f"{high}"
    p[34] = f"{low}"
    p[37] = f"{amt_wan}"
    return f'v_{sym}="' + "~".join(p) + '"'


def _simplified_line(sym: str, close: float = 10.5, prev: float = 10.0,
                     opn: float = 10.1) -> str:
    """★ 限流降级格式：前缀是 v_s_，字段数远少于 40。"""
    num = sym[2:]
    body = "~".join(["测试股", num, f"{close}", f"{prev}", f"{opn}"])
    return f'v_s_{sym}="{body}"'


def _batch(*syms, simplified=()) -> str:
    lines = []
    for s in syms:
        if s in simplified:
            lines.append(_simplified_line(s))
        else:
            lines.append(_full_line(s))
    return ";\n".join(lines) + ";"


# ---------------------------------------------------------------------------
# 1. ★ 核心：降级格式下沪市不再丢失（§0.4 根因）
# ---------------------------------------------------------------------------
def test_market_of_line_handles_simplified_prefix():
    """★ v_s_sh600004 必须被识别为沪市，不能落到 else 分支判成深市。"""
    mkt, num, simp = _market_of_line('v_s_sh600004="测试~600004~10.5~10.0~10.1"')
    assert mkt == "sh", "降级格式的沪市行被判成了深市 —— 这正是 §0.4 的根因"
    assert num == "600004"
    assert simp is True


def test_market_of_line_normal():
    assert _market_of_line('v_sh600004="..."') == ("sh", "600004", False)
    assert _market_of_line('v_sz000001="..."') == ("sz", "000001", False)
    assert _market_of_line('v_s_sz000001="..."') == ("sz", "000001", True)


def test_market_of_line_garbage():
    assert _market_of_line("not a response") == (None, None, False)
    assert _market_of_line("") == (None, None, False)


def test_shanghai_codes_survive_simplified_batch():
    """★ 整个批次以降级格式返回时，沪市代码必须仍然入账。"""
    syms = [f"sh60000{i}" for i in range(5)] + [f"sz00000{i}" for i in range(5)]
    sym_map = {s: ("1." if s.startswith("sh") else "0.") + s[2:] for s in syms}
    text = _batch(*syms, simplified=set(syms))  # 全部降级

    stats = FetchStats(vendor="tencent-qt")
    rows = parse_qt_batch_response(text, sym_map, stats)

    got = {r["code"] for r in rows}
    sh_got = {c for c in got if c.startswith("1.")}
    assert len(sh_got) == 5, (
        f"降级格式下沪市仅入账 {len(sh_got)}/5 —— §0.4 故障复现！"
        f"（旧实现会全部丢弃）"
    )
    assert stats.dropped_unknown_code == 0, "不应有 unknown_code 丢弃"
    # 每条降级行都留痕（10 条 = 5 沪 + 5 深）
    assert stats.dropped_short_format == 10, "每条降级格式都应留痕（不是丢弃）"
    assert len(rows) == 10, "降级格式下 10 只都应入账"


def test_old_implementation_would_lose_shanghai():
    """对照测试：模拟旧实现的二值判定，证明它会丢沪市。"""
    line = 'v_s_sh600004="测试~600004~10.5~10.0~10.1"'
    sym = "600004"
    # 旧实现的判定（有 bug）
    old_mkt = "sh" if line.startswith("v_sh") else "sz"
    assert old_mkt == "sz", "旧实现在降级格式下判定为深市（故障根因）"
    # 新实现的判定（已修复）
    new_mkt, _, _ = _market_of_line(line)
    assert new_mkt == "sh", "新实现正确判定为沪市"


def test_mixed_batch_all_kept():
    """混合批次：完整格式 + 降级格式都入账，且无静默丢弃。"""
    syms = [f"sh60000{i}" for i in range(6)]
    sym_map = {s: "1." + s[2:] for s in syms}
    text = _batch(*syms, simplified={syms[2], syms[4]})
    stats = FetchStats(vendor="tencent-qt")
    rows = parse_qt_batch_response(text, sym_map, stats)
    assert len(rows) == 6, f"应入账 6 只，得到 {len(rows)}"
    assert stats.dropped_unknown_code == 0


# ---------------------------------------------------------------------------
# 2. ★ 四条丢弃路径全部留痕（不再静默）
# ---------------------------------------------------------------------------
def test_short_format_counted_not_silent():
    """字段数不足 -> 计数（不再静默 continue）。"""
    short = 'v_sh600004="' + "~".join(["a"] * (SNAPSHOT_MIN_FIELDS - 1)) + '"'
    stats = FetchStats(vendor="tencent-qt")
    rows = parse_qt_batch_response(short, {"sh600004": "1.600004"}, stats)
    assert rows == []
    # 若判定为"非简化但字段不足" -> short_format 计数
    assert stats.dropped_short_format >= 1 or stats.dropped_parse_error >= 1
    assert stats.dropped_total >= 1, "字段不足必须留痕，不能静默"


def test_unknown_code_counted():
    """代码不在 sym_map -> 计数（原实现静默 continue）。"""
    text = _full_line("sh600004")
    stats = FetchStats(vendor="tencent-qt")
    rows = parse_qt_batch_response(text, {"sh999999": "1.999999"}, stats)
    assert rows == []
    assert stats.dropped_unknown_code == 1, "unknown_code 必须计数"


def test_parse_error_counted():
    """非数值字段 -> 计数（原实现 silently continue）。"""
    p = ["", "测试", "600004", "not_a_number", "10.0"] + [""] * 45
    text = 'v_sh600004="' + "~".join(p) + '"'
    stats = FetchStats(vendor="tencent-qt")
    rows = parse_qt_batch_response(text, {"sh600004": "1.600004"}, stats)
    assert rows == []
    assert stats.dropped_parse_error >= 1


def test_zero_close_counted():
    p = [""] * 50
    p[1], p[2], p[3], p[4], p[5] = "测试", "600004", "0", "10.0", "10.0"
    p[33], p[34], p[37] = "0", "0", "100"
    text = 'v_sh600004="' + "~".join(p) + '"'
    stats = FetchStats(vendor="tencent-qt")
    rows = parse_qt_batch_response(text, {"sh600004": "1.600004"}, stats)
    assert rows == []
    assert stats.dropped_parse_error >= 1


def test_stats_breakdown_complete():
    """FetchStats 必须暴露四条路径的完整分解（方案只列 3 条，我们补第 4 条）。"""
    s = FetchStats(vendor="t")
    s.dropped_batch_exc = 1
    s.dropped_short_format = 2
    s.dropped_unknown_code = 3
    s.dropped_parse_error = 4
    d = s.to_dict()["dropped_breakdown"]
    assert d == {"batch_exception": 1, "short_format": 2, "unknown_code": 3,
                 "parse_error": 4, "intraday": 0, "other": 0}
    assert s.dropped_total == 10


# ---------------------------------------------------------------------------
# 3. amount 单位换算（陷阱 1）
# ---------------------------------------------------------------------------
def test_amount_unit_converted_from_wan():
    """★ 腾讯 parts[37] 是万元 -> 必须换算成元（×1e4）。"""
    text = _full_line("sh600004", amt_wan=1300.5)
    stats = FetchStats(vendor="tencent-qt")
    rows = parse_qt_batch_response(text, {"sh600004": "1.600004"}, stats)
    assert rows[0]["amount"] == pytest.approx(1300.5e4)


# ---------------------------------------------------------------------------
# 4. L2 令牌桶
# ---------------------------------------------------------------------------
def test_token_bucket_acquire():
    import time

    b = TokenBucket(rate=50.0, burst=1)
    t0 = time.monotonic()
    assert b.acquire(1.0)
    b.acquire(1.0)
    assert time.monotonic() - t0 < 1.0


def test_backoff_grows_and_jitters():
    b = TokenBucket(rate=10)
    d0, d3 = b.backoff(0, base=0.1), b.backoff(3, base=0.1)
    assert d3 > d0
    assert d3 <= 30.0 * 1.3


# ---------------------------------------------------------------------------
# 5. L5 熔断
# ---------------------------------------------------------------------------
def test_circuit_opens_after_threshold():
    cb = CircuitBreaker("t", threshold=20)
    for _ in range(19):
        assert not cb.record_fail()
    assert cb.record_fail() is True
    assert cb.is_open


def test_circuit_slow_mode_within_2h():
    cb = CircuitBreaker("t", threshold=1)
    cb.record_fail()
    assert cb.slow_mode(), "刚熔断 -> 应走低频慢速模式"


def test_circuit_resets_on_success():
    cb = CircuitBreaker("t", threshold=5)
    for _ in range(4):
        cb.record_fail()
    cb.record_ok()
    assert cb.fails == 0
    assert not cb.is_open


def test_circuit_store_roundtrip(tmp_path):
    p = str(tmp_path / "circuit.json")
    st = CircuitStore(p)
    cb = st.get("tencent", threshold=3)
    cb.record_fail(); cb.record_fail(); cb.record_fail()
    st.put(cb)
    st.save()

    st2 = CircuitStore(p)
    cb2 = st2.get("tencent", threshold=3)
    assert cb2.fails == 3
    assert cb2.is_open


def test_circuit_cross_run_slow_mode(tmp_path):
    """★ 跨 run：上轮熔断且 <2h -> 慢速模式。"""
    p = str(tmp_path / "circuit.json")
    st = CircuitStore(p)
    cb = st.get("eastmoney", threshold=1)
    cb.record_fail()
    st.put(cb); st.save()
    st2 = CircuitStore(p)
    assert st2.get("eastmoney").slow_mode()


# ---------------------------------------------------------------------------
# 6. L2 请求预算
# ---------------------------------------------------------------------------
def test_budget_exceeded_raises_and_dumps(tmp_path):
    prog = str(tmp_path / "progress.json")
    b = RequestBudget(limit=3, progress_path=prog)
    for _ in range(3):
        b.spend()
    with pytest.raises(RequestBudgetExceeded, match="预算超限"):
        b.spend()
    assert os.path.exists(prog), "超限必须落盘进度文件供断点续跑"


def test_budget_remaining():
    b = RequestBudget(limit=10)
    b.spend(4)
    assert b.remaining == 6


# ---------------------------------------------------------------------------
# 7. L1 多源互备编排
# ---------------------------------------------------------------------------
def _mk_vendor(name, fail_first_n=0, drop=()):
    calls = {"n": 0}

    def fetch(batch):
        calls["n"] += 1
        out = []
        for c in batch:
            if c in drop:
                continue
            if calls["n"] <= fail_first_n:
                raise RuntimeError("batch failed")
            out.append({"code": c, "date": None, "open": 1.0, "close": 1.0,
                        "high": 1.0, "low": 1.0, "volume": 1, "amount": 1.0})
        return out

    v = Vendor(name=name, fetch=fetch, bucket=TokenBucket(1000, 10))
    v.calls = calls
    return v


def test_fallback_switches_to_backup():
    """主源失败 -> 切备源，最终拿到全部数据。"""
    primary = _mk_vendor("primary", fail_first_n=999)     # 永远失败
    backup = _mk_vendor("backup")
    rows, stats = fetch_with_fallback(
        [primary, backup], ["1.600001", "1.600002"],
        batch_size=2, policy=RetryPolicy(batch_retries=1, base_delay=0.001))
    assert len(rows) == 2, "备源应补齐主源失败的部分"
    assert "primary" in stats.circuits_opened


def test_partial_success_not_rolled_back():
    """★ 部分成功不是失败：落盘成功的部分 + missing_codes 记录缺口。"""
    v = _mk_vendor("only", drop=("1.600003",))
    rows, stats = fetch_with_fallback(
        [v], ["1.600001", "1.600002", "1.600003"], batch_size=3,
        policy=RetryPolicy(batch_retries=1, base_delay=0.001))
    codes = {r["code"] for r in rows}
    assert codes == {"1.600001", "1.600002"}, "成功部分不得因部分失败而回滚"
    assert stats.missing_codes == ["1.600003"]


def test_all_vendors_circuit_open_returns_empty():
    vs = []
    for n in ("a", "b"):
        v = _mk_vendor(n)
        v.breaker.opened_at = __import__("time").time()
        vs.append(v)
    rows, stats = fetch_with_fallback(vs, ["1.600001"])
    assert rows == []
    assert set(stats.circuits_opened) == {"a", "b"}


def test_budget_blocks_runaway():
    """★ 封死"误判除权 -> 整段重拉 -> 打爆限流"的雪崩路径。

    构造：主源"部分成功"（不触发批次整体失败），因此会持续跑到预算耗尽。
    """
    calls = {"n": 0}

    def slow_partial(batch):
        calls["n"] += 1
        # 每批只成功 1 只 -> 不触发批次整体失败 -> 持续消耗预算
        return [{"code": batch[0], "date": None, "open": 1.0, "close": 1.0,
                 "high": 1.0, "low": 1.0, "volume": 1, "amount": 1.0}]

    v = Vendor(name="slow", fetch=slow_partial, bucket=TokenBucket(10000, 100))
    with pytest.raises(RequestBudgetExceeded):
        fetch_with_fallback(
            [v], [f"1.6000{i:02d}" for i in range(50)], batch_size=1,
            budget=RequestBudget(limit=5),
            policy=RetryPolicy(batch_retries=0, base_delay=0.001))


def test_fail_fast_on_batch_exhaustion():
    """★ 批次重试耗尽即让出备源，不在一棵树上吊死（不需要等熔断阈值）。"""
    primary = _mk_vendor("primary", fail_first_n=999)
    backup = _mk_vendor("backup")
    rows, stats = fetch_with_fallback(
        [primary, backup], [f"1.6000{i:02d}" for i in range(10)], batch_size=5,
        policy=RetryPolicy(batch_retries=1, base_delay=0.001))
    assert len(rows) == 10, "备源应补齐"
    assert primary.breaker.fails == 1, "主源第 1 批耗尽后应立刻让出，不再试后续批次"
    assert calls_of(primary) <= 2, f"主源请求次数应很少，实际 {calls_of(primary)}"


def calls_of(v):
    return v.calls["n"]


# ---------------------------------------------------------------------------
# 8. 保守默认值
# ---------------------------------------------------------------------------
def test_merged_default_conservative():
    from common.datasource import assert_merged_default_is_conservative

    assert assert_merged_default_is_conservative()
