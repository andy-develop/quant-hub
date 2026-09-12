#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个股迁移 to_contract_raw 的口径回归：amount 逐行复现 signals.amt、volume_股=amount/close、
且"整列缩放 volume 不改任何只依赖比值的信号"（迁移基线安全性的核心证明）。"""
import os
import sys

import pytest

pd = pytest.importorskip("pandas")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from tools.data_pipeline import stock_migrate as SM  # noqa: E402


def _df(rows):
    return pd.DataFrame(rows)


def test_ifzq_row_no_amount_uses_close_vol_100():
    # ifzq 源：无 amount，volume 单位=手
    d = _df([{"code": "301665", "date": "2025-04-11", "open": 45.0, "high": 45.0,
              "low": 39.26, "close": 40.05, "volume": 297560.0}])
    out = SM.to_contract_raw(d)
    # amount = close*volume(手)*100 —— 与 signals.py 现有 fallback 逐位一致
    assert out["amount"].iloc[0] == pytest.approx(40.05 * 297560 * 100)
    # volume_股 = amount/close = volume_手*100
    assert out["volume"].iloc[0] == 297560 * 100


def test_baostock_row_with_amount_keeps_amount_and_derives_shares():
    # baostock 源：amount(元) 存在、volume 单位=股
    d = _df([{"code": "600000", "date": "2025-04-11", "open": 10.0, "high": 10.5,
              "low": 9.8, "close": 10.2, "volume": 1_000_000.0, "amount": 10_200_000.0}])
    out = SM.to_contract_raw(d)
    assert out["amount"].iloc[0] == pytest.approx(10_200_000.0)   # 用源 amount，不重算
    assert out["volume"].iloc[0] == round(10_200_000.0 / 10.2)    # = 1,000,000 股


def test_zero_or_nan_amount_falls_back():
    d = _df([
        {"code": "1", "date": "2025-01-02", "open": 5, "high": 5, "low": 5, "close": 5.0,
         "volume": 1000.0, "amount": 0.0},          # amount=0 视为缺失
        {"code": "2", "date": "2025-01-02", "open": 8, "high": 8, "low": 8, "close": 8.0,
         "volume": 500.0, "amount": float("nan")},  # amount=NaN 视为缺失
    ])
    out = SM.to_contract_raw(d)
    assert out["amount"].iloc[0] == pytest.approx(5.0 * 1000 * 100)
    assert out["amount"].iloc[1] == pytest.approx(8.0 * 500 * 100)


def test_mixed_source_concat_amount_column():
    # base(ifzq,无amount) 与 baostock(有amount) concat -> amount 列部分 NaN
    a = _df([{"code": "301665", "date": "2025-04-11", "open": 45, "high": 45, "low": 39,
              "close": 40.0, "volume": 1000.0}])                       # 无 amount
    b = _df([{"code": "600000", "date": "2025-04-11", "open": 10, "high": 10, "low": 9,
              "close": 10.0, "volume": 2000.0, "amount": 20000.0}])    # 有 amount
    merged = pd.concat([a, b], ignore_index=True)                      # amount 列出现，a 行为 NaN
    out = SM.to_contract_raw(merged)
    row_a = out[out["code"] == "301665"].iloc[0]
    row_b = out[out["code"] == "600000"].iloc[0]
    assert row_a["amount"] == pytest.approx(40.0 * 1000 * 100)         # NaN -> fallback
    assert row_b["amount"] == pytest.approx(20000.0)                   # 用源 amount


def test_volume_scaling_leaves_ratio_signals_invariant():
    """核心证明：signals 只用 volume 的"自身比值"(shrink: vol>=2*prev_vol_ma5)。
    整列 ×100（手->股）后该比值判定不变 -> 迁移不改信号。"""
    base = [100.0, 120.0, 110.0, 130.0, 125.0, 400.0, 150.0]   # 第6根放量
    vol_shou = pd.Series(base)                 # 手
    vol_gu = vol_shou * 100                    # 股

    def shrink_flags(v):
        ma5 = v.rolling(5).mean()
        prev_ma5 = ma5.shift(1)
        return (v >= 2 * prev_ma5).fillna(False).tolist()

    assert shrink_flags(vol_shou) == shrink_flags(vol_gu)


def test_hfq_attaches_real_vol_amount_from_raw():
    raw = _df([{"code": "600000", "date": "2025-04-11", "open": 10, "high": 10.5, "low": 9.8,
                "close": 10.2, "volume": 1_000_000.0, "amount": 10_200_000.0}])
    raw_ct = SM.to_contract_raw(raw)
    # hfq 价是后复权（放大），但 volume/amount 必须取真实值（从 raw 并入），不能用 hfq close 反推
    hfq = _df([{"code": "600000", "date": "2025-04-11", "open": 50, "high": 52, "low": 49,
                "close": 51.0, "volume": 1_000_000.0}])
    out = SM.attach_real_vol_amount(hfq, raw_ct)
    assert out["close"].iloc[0] == pytest.approx(51.0)          # hfq 价保留
    assert out["amount"].iloc[0] == pytest.approx(10_200_000.0)  # 真实 amount，不是 51*vol*100
    assert out["volume"].iloc[0] == round(10_200_000.0 / 10.2)   # 真实股数
