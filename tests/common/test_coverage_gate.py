"""覆盖率硬门禁测试 —— 用 §0.4 的真实数字回归。

方案 §0.4 的实测基线：
    日期           入库    沪市（2316）      深市（2899）
    2026-09-04     5015    2234（99.4%）     2781（95.9%）  正常
    2026-09-07    2929     39（1.7%）        2890（99.7%）  故障
    2026-09-10    2925     39（1.7%）        2886（99.6%）  故障

★ 关键断言：故障日的**整体覆盖率约 55%**（2925/5215），
  若门禁只看整体，55% < 80% 会红 —— 但其实门禁只要设对阈值即可捕获。
  更危险的反面：若整体正常但单市场缺失（如深市 99% + 沪市 1.7% = 55%），
  单看整体也需捕获。这里用真实数字验证两条路径都能红。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from common.gates.coverage import (  # noqa: E402
    FAIL_THRESHOLD,
    GREEN,
    RED,
    WARN_THRESHOLD,
    YELLOW,
    CoverageGateError,
    check_coverage,
)

# ★ 方案 §0.4 实测：在市标的数
SH_TOTAL = 2316
SZ_TOTAL = 2899
ALL_TOTAL = SH_TOTAL + SZ_TOTAL  # 5215


def _universe():
    """构造在市标的（沪 2316 + 深 2899）。"""
    sh = [f"6{i:05d}" for i in range(SH_TOTAL)]
    sz = [f"0{i:05d}" for i in range(SZ_TOTAL)]
    return sh + sz


def _codes(sh_n, sz_n, prefix_sh="6", prefix_sz="0"):
    sh = [f"{prefix_sh}{i:05d}" for i in range(sh_n)]
    sz = [f"{prefix_sz}{i:05d}" for i in range(sz_n)]
    return sh + sz


# ---------------------------------------------------------------------------
# 1. 正常日
# ---------------------------------------------------------------------------
def test_normal_day_is_green():
    """§0.4 正常基线：沪 99.4% / 深 95.9% -> 绿。"""
    got = _codes(2302, 2781)   # ≈ 99.4% / 95.9%
    res = check_coverage(got, _universe(), asset="stock", day="2026-09-04")
    assert res.level == GREEN, res.summary()
    assert res.by_market["sh"] > 0.99
    assert res.by_market["sz"] > 0.95


# ---------------------------------------------------------------------------
# 2. ★ 故障日必须红（§0.4 的真实复现）
# ---------------------------------------------------------------------------
def test_fault_day_shanghai_1p7pct_is_red():
    """★ 09-07~10 的真实数字：沪市 1.7%，深市 99.6%，整体 ~55%。"""
    got = _codes(39, 2886)
    with pytest.raises(CoverageGateError) as ei:
        check_coverage(got, _universe(), asset="stock", day="2026-09-07")
    msg = str(ei.value)
    assert "1.7%" in msg or "39" in msg
    assert "沪" in msg or "sh" in msg


def test_fault_day_would_pass_if_only_overall_checked():
    """★ 反面论证：单纯看整体会漏。构造"沪市全丢、深市全有"。

    沪 0% + 深 100% -> 整体 55.6%。若门禁只判 overall < 80% 会红（这条 OK），
    但若阈值被误设成 50%，就会漏掉整市场缺失 —— 所以必须分市场判定。
    """
    got = _codes(0, SZ_TOTAL)
    res = check_coverage(got, _universe(), asset="stock", day="2026-09-07",
                         raise_on_red=False)
    assert res.level == RED
    assert res.by_market["sh"] == 0.0
    assert res.by_market["sz"] == pytest.approx(1.0)


def test_per_market_floor_catches_shanghai_loss_with_high_overall():
    """★ 分市场下限的真正价值：整体 96% 但某市场全丢。"""
    # 沪 0% + 深 100%，但沪占比小 -> 整体仍高
    uni = _codes(100, 2400)          # 沪 100 只（少量），深 2400 只
    got = _codes(0, 2400)            # 沪全丢
    res = check_coverage(got, uni, asset="stock", day="2026-09-11",
                         raise_on_red=False)
    assert res.overall > 0.95, f"整体应仍很高，实际 {res.overall:.1%}"
    assert res.level == RED, "整体高但单市场全丢 -> 必须红（分市场下限）"
    assert res.by_market["sh"] == 0.0


def test_per_market_floor_can_be_disabled():
    uni = _codes(100, 2400)
    got = _codes(0, 2400)
    res = check_coverage(got, uni, asset="stock", day="2026-09-11",
                         raise_on_red=False, per_market_floor=False)
    assert res.level == GREEN, "关闭分市场判定后，整体高即绿"


# ---------------------------------------------------------------------------
# 3. 阈值分界
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ratio,want", [
    (1.00, GREEN),
    (0.96, GREEN),
    (0.95, GREEN),      # 边界：>= warn 为绿
    (0.949, YELLOW),
    (0.85, YELLOW),
    (0.80, YELLOW),     # 边界：>= fail 为黄
    (0.799, RED),
    (0.50, RED),
    (0.00, RED),
])
def test_threshold_boundaries(ratio, want):
    uni = _codes(1000, 1000)
    n = int(2000 * ratio)
    # 按比例分配到两个市场，保证分市场也同比例
    got = _codes(n // 2, n - n // 2)
    res = check_coverage(got, uni, asset="stock", day="2026-09-11",
                         raise_on_red=False)
    assert res.level == want, f"ratio={ratio} -> {res.level} (期望 {want})"


def test_thresholds_match_plan():
    """方案 §0.4：<95% 黄 / <80% 红。"""
    assert WARN_THRESHOLD == 0.95
    assert FAIL_THRESHOLD == 0.80


# ---------------------------------------------------------------------------
# 4. raise 语义（红 -> 中止不落盘）
# ---------------------------------------------------------------------------
def test_red_raises_by_default():
    got = _codes(10, 10)
    with pytest.raises(CoverageGateError, match="覆盖率红"):
        check_coverage(got, _universe(), asset="stock", day="2026-09-11")


def test_yellow_does_not_raise():
    """黄：发布但标黄，不 abort。"""
    uni = _codes(1000, 1000)
    got = _codes(900, 900)   # 90% -> 黄
    res = check_coverage(got, uni, asset="stock", day="2026-09-11")
    assert res.level == YELLOW
    assert res.ok_to_write


def test_no_raise_flag():
    got = _codes(10, 10)
    res = check_coverage(got, _universe(), asset="stock", day="2026-09-11",
                         raise_on_red=False)
    assert res.level == RED
    assert not res.ok_to_write


# ---------------------------------------------------------------------------
# 5. 空 universe 防护
# ---------------------------------------------------------------------------
def test_empty_universe_is_red():
    """universe 为空 -> 红（怀疑 universe 未加载），不能当成"全绿"。"""
    with pytest.raises(CoverageGateError, match="universe is empty"):
        check_coverage(["600000"], [], asset="stock", day="2026-09-11")


# ---------------------------------------------------------------------------
# 6. 报告内容
# ---------------------------------------------------------------------------
def test_result_reports_missing_sample():
    got = _codes(5, 5)
    res = check_coverage(got, _universe(), asset="stock", day="2026-09-11",
                         raise_on_red=False)
    assert res.missing_sample, "应给出缺失样本供排查"
    assert len(res.missing_sample) <= 20


def test_to_dict_shape():
    res = check_coverage(_codes(5, 5), _universe(), asset="stock",
                         day="2026-09-11", raise_on_red=False)
    d = res.to_dict()
    assert set(d) >= {"asset", "day", "level", "expected", "got", "overall",
                      "by_market", "missing_sample", "reasons"}
    assert d["asset"] == "stock"
    assert d["level"] == RED


def test_summary_readable():
    res = check_coverage(_codes(39, 2886), _universe(), asset="stock",
                         day="2026-09-07", raise_on_red=False)
    s = res.summary()
    assert "RED" in s and "1.7%" in s, s


# ---------------------------------------------------------------------------
# 7. 市场识别（含北交所）
# ---------------------------------------------------------------------------
def test_market_classification():
    from common.gates.coverage import _market_of

    assert _market_of("600000") == "sh"
    assert _market_of("688981") == "sh"
    assert _market_of("000001") == "sz"
    assert _market_of("300750") == "sz"
    assert _market_of("830799") == "bj"
    assert _market_of("430047") == "bj"
    assert _market_of("H20269") == "other"


def test_bj_market_separately_tracked():
    uni = _codes(100, 100) + [f"8{i:05d}" for i in range(50)]
    got = _codes(100, 100)          # 北交所全丢
    res = check_coverage(got, uni, asset="stock", day="2026-09-11",
                         raise_on_red=False)
    assert "bj" in res.by_market
    assert res.by_market["bj"] == 0.0
    assert res.level == RED


# ---------------------------------------------------------------------------
# 8. universe 兼容 DataFrame / list
# ---------------------------------------------------------------------------
def test_universe_dataframe_with_status():
    pd = pytest.importorskip("pandas")

    from common.gates.coverage import check_stock_coverage

    uni = pd.DataFrame({
        "code": ["600000", "600001", "000001", "000002"],
        "status": ["1", "1", "1", "0"],   # 000002 退市
    })
    got = ["600000", "600001", "000001"]
    res = check_stock_coverage(got, uni, day="2026-09-11")
    assert res.expected == 3, "status=0 的退市股不应计入在市集合"
    assert res.level == GREEN


def test_universe_list():
    from common.gates.coverage import check_stock_coverage

    res = check_stock_coverage(["600000"], ["600000", "600001"], day="2026-09-11",
                               raise_on_red=False)
    assert res.expected == 2
    assert res.overall == 0.5
    assert res.level == RED
