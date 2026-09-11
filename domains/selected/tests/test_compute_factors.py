#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个性化选股域最小回归测试（方案 §9.1：该域原本零测试）。

只覆盖三条最该锁住的行为，全部离线、纯标准库、不联网、不依赖真实行情：

  1. 因子数值正确性 —— 合成收盘价，与独立参考实现（statistics.pstdev）手算比对；
  2. 派生幂等性 —— 同一份 prices.json 连跑两次 main()，factors.json 逐字节一致；
  3. 空数据不崩 —— prices.json 为空对象 / 缺字段 / 数据不足时，安全跳过而非抛异常。

运行：pytest domains/selected/tests -q
"""
import json
import math
import os
import statistics
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import compute_factors as CF  # noqa: E402


# --------------------------------------------------------------------------
# 1. 因子数值正确性
# --------------------------------------------------------------------------
def test_pct_change_matches_reference():
    closes = [100.0, 110.0, 105.0, 120.0]
    got = CF.pct_change(closes)
    exp = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    assert got == pytest.approx(exp, rel=0, abs=1e-15)
    assert len(got) == len(closes) - 1


def test_momentum_hand_computed():
    # closes = 1..61（61 个点）
    closes = [float(i) for i in range(1, 62)]
    # 20 日动量 = 末值 / 末-20 值 - 1 = 61 / 41 - 1
    assert CF.calc_momentum(closes, 20) == pytest.approx(61.0 / 41.0 - 1, rel=1e-12)
    # 60 日动量 = 61 / 1 - 1 = 60
    assert CF.calc_momentum(closes, 60) == pytest.approx(60.0, rel=1e-12)


def test_momentum_returns_none_when_insufficient():
    # 长度必须严格大于窗口，否则 None（数据不足，上层跳过）
    assert CF.calc_momentum([1.0, 2.0], 20) is None
    assert CF.calc_momentum([float(i) for i in range(1, 22)], 20) is not None  # 21 > 20


def test_volatility_zero_for_constant_returns():
    # 等比数列 → 每日收益率恒定 → 标准差 0 → 年化波动率 0
    closes = [100.0 * (1.01 ** i) for i in range(25)]
    assert CF.calc_volatility(closes, 20) == pytest.approx(0.0, abs=1e-12)


def test_volatility_matches_population_std_reference():
    """与独立参考实现比对：总体标准差(pstdev) × sqrt(252)。

    compute_factors 用的是有偏(总体)方差 —— 除以 N 而非 N-1。这里用
    statistics.pstdev 复算，确认口径一致（防止有人误改成样本标准差）。
    """
    closes = [100.0, 102.0, 101.0, 105.0, 103.0, 108.0, 107.0, 110.0,
              109.0, 112.0, 111.0, 115.0, 114.0, 118.0, 117.0, 120.0,
              119.0, 123.0, 122.0, 126.0, 125.0]  # 21 个点
    rets = CF.pct_change(closes[-21:])
    expected = statistics.pstdev(rets) * math.sqrt(252)
    assert CF.calc_volatility(closes, 20) == pytest.approx(expected, rel=1e-12)


def test_volatility_none_when_insufficient():
    assert CF.calc_volatility([1.0, 2.0, 3.0], 20) is None


# --------------------------------------------------------------------------
# 2. 派生幂等性
# --------------------------------------------------------------------------
def _run_main(tmp_path, prices):
    pf = tmp_path / "prices.json"
    ff = tmp_path / "factors.json"
    pf.write_text(json.dumps(prices, ensure_ascii=False), encoding="utf-8")
    # 重定向模块级路径常量到临时目录（不污染仓库内 data/）
    old_p, old_f = CF.PRICES_FILE, CF.FACTORS_FILE
    CF.PRICES_FILE, CF.FACTORS_FILE = str(pf), str(ff)
    try:
        CF.main()
        return ff.read_bytes()
    finally:
        CF.PRICES_FILE, CF.FACTORS_FILE = old_p, old_f


def _sample_prices():
    # 61 个交易日的合成收盘价，足够算 20/60 日动量与 20 日波动率
    closes = [100.0 + i * 0.7 + (i % 5) for i in range(61)]
    return {"600000": {"closes": closes}, "000001": {"closes": closes}}


def test_derivation_is_idempotent(tmp_path):
    prices = _sample_prices()
    b1 = _run_main(tmp_path, prices)
    b2 = _run_main(tmp_path, prices)
    assert b1 == b2  # 逐字节一致
    factors = json.loads(b1.decode("utf-8"))
    assert set(factors) == {"600000", "000001"}
    for v in factors.values():
        assert set(v) == {"close", "mom_20", "mom_60", "vol_20"}
        assert v["mom_20"] is not None and v["vol_20"] is not None


# --------------------------------------------------------------------------
# 3. 空数据 / 数据不足不崩
# --------------------------------------------------------------------------
def test_empty_prices_no_crash(tmp_path):
    out = _run_main(tmp_path, {})
    assert json.loads(out.decode("utf-8")) == {}


def test_insufficient_history_skipped(tmp_path):
    # 只有 5 个点 < 21 → 该标的被跳过，不写入 factors，也不抛异常
    out = _run_main(tmp_path, {"600000": {"closes": [1.0, 2.0, 3.0, 4.0, 5.0]}})
    assert json.loads(out.decode("utf-8")) == {}


def test_missing_closes_field_skipped(tmp_path):
    # 缺 closes 字段 → get("closes", []) 得到空列表 → 安全跳过
    out = _run_main(tmp_path, {"600000": {"name": "浦发银行"}})
    assert json.loads(out.decode("utf-8")) == {}


def test_main_exits_when_prices_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(CF, "PRICES_FILE", str(tmp_path / "does_not_exist.json"))
    with pytest.raises(SystemExit) as ei:
        CF.main()
    assert ei.value.code == 1
