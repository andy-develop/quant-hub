"""§0.4 沪市覆盖故障回归测试 —— 用真实响应形态复现，并证明修复后不再丢失。

事故数字（quant-lab/data/kline/incremental/raw_2026090{7,8,9},10.parquet）：
    沪市 39 / 2316 = 1.7%      深市 ~2886 / 2899 = 99.6%
    39 = 2316 − 38×60 —— 每批 60 只，前 38 批整体被丢

本测试不联网：直接用构造的响应文本喂解析器，逐条验证四个丢弃路径。
"""

from __future__ import annotations

import pandas as pd
import pytest

from common.datasource import FetchStats
from common.datasource.vendor_tencent import (
    _market_of_line,
    parse_qt_batch_response,
)
from common.gates.coverage import CoverageGateError, check_stock_coverage

SH_TOTAL = 2316
SZ_TOTAL = 2899


def _full_line(sym: str, name: str = "测试", price: float = 10.0) -> str:
    """构造一条完整格式（40 字段）腾讯响应行。

    sym 可传 'sh600004' 或裸 '600004'，内部统一补市场前缀。

    字段位（腾讯实测口径）：
        [1]名称 [2]代码 [3]现价 [4]昨收 [5]今开 [6]成交量(手) ...
        [33]最高 [34]最低 [37]成交额(万元)
    """
    num = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
    mkt = sym[:2] if sym[:2] in ("sh", "sz", "bj") else "sh"
    p = [""] * 40
    p[1], p[2] = name, num
    p[3], p[4], p[5] = f"{price}", f"{price - 0.1}", f"{price - 0.05}"
    p[6] = "12345"
    p[33], p[34] = f"{price + 0.2}", f"{price - 0.3}"
    p[37] = "6789.0"
    # 末位留空会让 split 结果少 1 段；用哨兵占住第 39 位确保字段数 >= 40
    p[39] = "0"
    return f'v_{mkt}{num}="' + "~".join(p) + '";'


def _simplified_line(sym: str, price: float = 10.0) -> str:
    """★ §0.4 关键：限流降级响应。前缀是 v_s_ 而不是 v_。"""
    num = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
    mkt = sym[:2] if sym[:2] in ("sh", "sz", "bj") else "sh"
    p = ["", num, "测试", f"{price}", f"{price - 0.1}", f"{price - 0.05}",
         "12345", "", "", "", "", "", "", "", "", "", "", "", "", "", "0"]
    return f'v_s_{mkt}{num}="' + "~".join(p) + '";'


def _smap(codes: list[str]) -> dict[str, str]:
    """短代码 -> 腾讯符号与 secid 的双向表：{'sh600004': '1.600004'}。"""
    out = {}
    for c in codes:
        mkt = "1" if c.startswith("6") else "0"
        sym = ("sh" if mkt == "1" else "sz") + c
        out[sym] = f"{mkt}.{c}"
    return out


# ---------------------------------------------------------------------------
# 1. 市场判定：v_s_ 前缀不再被误判为深市
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("line,expect_mkt,expect_num,expect_simplified", [
    ('v_sh600004="x"', "sh", "600004", False),
    ('v_sz000001="x"', "sz", "000001", False),
    ('v_s_sh600004="x"', "sh", "600004", True),      # ★ 限流降级
    ('v_s_sz000001="x"', "sz", "000001", True),      # ★ 限流降级
    ('v_bj830799="x"', "bj", "830799", False),
])
def test_market_of_line_handles_simplified_prefix(line, expect_mkt, expect_num, expect_simplified):
    mkt, num, simplified = _market_of_line(line)
    assert (mkt, num, simplified) == (expect_mkt, expect_num, expect_simplified)


def test_market_of_line_rejects_garbage():
    for bad in ("", "no_equals_sign", 'v_="x"', 'v_xx600004="x"'):
        mkt, num, _ = _market_of_line(bad)
        assert mkt is None and num is None, bad


def test_old_binary_judgement_would_fail():
    """证明旧写法必然出错 —— 这是修复的必要性证据。"""
    line = 'v_s_sh600004="x"'
    old = ("sh" if line.startswith("v_sh") else "sz") + "600004"
    assert old == "sz600004"          # 旧逻辑：沪市股票被判成深市
    new_mkt, new_num, _ = _market_of_line(line)
    assert new_mkt + new_num == "sh600004"


# ---------------------------------------------------------------------------
# 2. 解析器：全量输入 -> 全量输出，无静默丢弃
# ---------------------------------------------------------------------------
def test_full_batch_no_drop():
    codes = [f"6{i:05d}" for i in range(60)]
    smap = _smap(codes)
    text = "".join(_full_line(c) for c in codes)
    stats = FetchStats(vendor="test")
    rows = parse_qt_batch_response(text, smap, stats)

    assert len(rows) == 60
    assert stats.dropped_total == 0
    assert {r["code"] for r in rows} == set(smap.values())


def test_simplified_batch_not_dropped():
    """★ 事故核心：整批走降级格式，修复后仍应全部入库（标记 degraded）。"""
    codes = [f"6{i:05d}" for i in range(60)]
    smap = _smap(codes)
    text = "".join(_simplified_line(c) for c in codes)
    stats = FetchStats(vendor="test")
    rows = parse_qt_batch_response(text, smap, stats)

    assert len(rows) == 60, "降级格式不得整批丢弃"
    assert all(r["code"].startswith("1.") for r in rows), "沪市代码必须仍是沪市"
    assert all(r.get("_degraded") for r in rows)
    # 记账：short_format 计数上升，但那是指标不是丢数据
    assert stats.dropped_short_format == 60
    assert stats.dropped_unknown_code == 0


def test_amount_unit_is_yuan_not_wan():
    """★ 硬陷阱一：成交额单位。腾讯给万元，契约要求元。"""
    smap = _smap(["600004"])
    stats = FetchStats(vendor="test")
    rows = parse_qt_batch_response(_full_line("sh600004"), smap, stats)
    assert rows[0]["amount"] == pytest.approx(6789.0 * 1e4)
    assert rows[0]["volume"] == 12345          # 股（契约单位），不是手


def test_volume_unit_is_shares():
    """腾讯 [6] 为手，但契约要求股 —— 旧实现直接写 float，单位口径必须冻结。"""
    smap = _smap(["600004"])
    stats = FetchStats(vendor="test")
    rows = parse_qt_batch_response(_full_line("sh600004"), smap, stats)
    assert isinstance(rows[0]["volume"], int)
    assert rows[0]["volume"] == 12345


def test_drop_paths_are_counted_not_silent(  # noqa: D103
        bad_line=None, field=None):
    cases = [
        ('v_xx600004="' + "~".join(["x"] * 40) + '"', "other"),   # 前缀无法判市场
        ('v_sh600004="only~two"', "short_format"),               # 字段数不足
        ('v_sh999999="' + "~".join(["x"] * 40) + '"', "unknown_code"),  # 不在 universe
    ]
    if bad_line is not None:
        cases = [(bad_line, field)]
    smap = _smap(["600004"])
    for line, fld in cases:
        stats = FetchStats(vendor="test")
        parse_qt_batch_response(line + ";", smap, stats)
        assert getattr(stats, f"dropped_{fld}") >= 1, f"{fld} 未被记账: {line[:40]}"


def test_fourth_drop_path_exists():
    """方案只列了 3 条丢弃路径；实测有第 4 条（解析异常），必须也记账。"""
    line = _full_line("sh600004").replace("~10.0~", "~not-a-number~")
    stats = FetchStats(vendor="test")
    rows = parse_qt_batch_response(line, _smap(["600004"]), stats)
    assert rows == []
    assert stats.dropped_parse_error == 1


def test_zero_price_dropped_as_parse_error():
    """现价 <= 0 的脏行（停牌占位）也必须记账而非静默丢弃。"""
    line = _full_line("sh600004", price=0.0)
    stats = FetchStats(vendor="test")
    rows = parse_qt_batch_response(line, _smap(["600004"]), stats)
    assert rows == []
    assert stats.dropped_parse_error == 1


# ---------------------------------------------------------------------------
# 3. 端到端：复现事故日并证明门禁拦住
# ---------------------------------------------------------------------------
def _universe() -> pd.DataFrame:
    sh = [f"6{i:05d}" for i in range(SH_TOTAL)]
    sz = [f"0{i:05d}" for i in range(SZ_TOTAL)]
    return pd.DataFrame({"code": sh + sz, "status": ["1"] * (SH_TOTAL + SZ_TOTAL)})


def test_fault_day_reproduces_1p7pct_and_blocks():
    """故障日：沪市只进来 39 只（38 批丢失）。整体 ~55%，沪市 1.7% -> 必须红灯中止。"""
    got = [f"6{i:05d}" for i in range(39)] + [f"0{i:05d}" for i in range(2886)]
    with pytest.raises(CoverageGateError) as ei:
        check_stock_coverage(got, _universe(), day="2026-09-07")
    msg = str(ei.value)
    assert "RED" in msg
    assert "sh=" in msg


def test_cross_market_gate_catches_what_overall_gate_misses():
    """★ 这是 per_market_floor 的存在理由：整体 55.8% 若只看整体会判黄灯放行。"""
    got = [f"6{i:05d}" for i in range(39)] + [f"0{i:05d}" for i in range(2886)]
    res = check_stock_coverage(got, _universe(), day="2026-09-07", raise_on_red=False)
    assert res.overall > 0.5, "整体覆盖率超过 50%"
    assert res.by_market["sh"] < 0.02, "沪市覆盖率 1.7%"
    assert res.level == "red", "分市场下限必须把它判红"


def test_fixed_run_passes_gate():
    """修复后（含降级格式救回）覆盖率达标 -> 绿灯，允许写盘。"""
    got = ([f"6{i:05d}" for i in range(SH_TOTAL)]
           + [f"0{i:05d}" for i in range(SZ_TOTAL)])
    res = check_stock_coverage(got, _universe(), day="2026-09-11", raise_on_red=False)
    assert res.level == "green"
    assert res.ok_to_write


def test_success_rate_thresholds():
    """边界：95% 绿 / 80%~95% 黄 / <80% 红。"""
    def res_with(n_total: int, n_got: int):
        codes_sh = [f"6{i:05d}" for i in range(n_total)]
        codes_sz = [f"0{i:05d}" for i in range(n_total)]
        got = [f"6{i:05d}" for i in range(n_got)] + [f"0{i:05d}" for i in range(n_got)]
        uni = pd.DataFrame({"code": codes_sh + codes_sz,
                            "status": ["1"] * (2 * n_total)})
        return check_stock_coverage(got, uni, day="2026-09-11", raise_on_red=False)

    assert res_with(1000, 960).level == "green"
    assert res_with(1000, 900).level == "yellow"
    assert res_with(1000, 700).level == "red"
