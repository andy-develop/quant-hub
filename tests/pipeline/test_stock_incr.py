#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_incr 除权检测回归（离线，不联网）。

latest_closes 的返回值契约是 {code: (date, close)} 元组（build_ratio 同款解包），
而 detect_dividends 曾直接 `pc / prev` 把元组当数值除 -> TypeError，
2026-09-14 起 data-stock-incr 连续红 5 天。这里锁住元组处理 + 保险丝闸。

运行：pytest tests/pipeline -q
"""
import pytest

pd = pytest.importorskip("pandas")

from tools.data_pipeline import stock_incr as SI  # noqa: E402


def test_detect_dividends_accepts_tuple_prev():
    """prev_raw 为 {code: (date, close)} 元组时不再 TypeError，且偏差判对。"""
    snap = [
        {"code": "600000", "prev_close": 10.04},   # 库内 10.00 -> +0.4% < 容差，不标
        {"code": "000001", "prev_close": 20.0},    # 库内 20.0  -> 0%，不标
        {"code": "830799", "prev_close": 5.0},     # 库内缺失  -> 跳过
        {"code": "601988", "prev_close": 3.18},    # 库内 3.00 -> +6.0%，除权
    ]
    prev_raw = {
        "600000": ("2026-09-17", 10.0),
        "000001": ("2026-09-17", 20.0),
        "601988": ("2026-09-17", 3.0),
    }
    div = SI.detect_dividends(snap, prev_raw)
    assert div == ["601988"]


def test_detect_dividends_fuse_guard():
    """过半股票同时被标除权 -> 保险丝触发中止（交易日错位保护，§0.4 教训）。"""
    snap = [{"code": f"{i:06d}", "prev_close": 5.0} for i in range(100)]
    prev_raw = {f"{i:06d}": ("2026-09-17", 4.0) for i in range(100)}   # 全部 +25%
    with pytest.raises(RuntimeError):
        SI.detect_dividends(snap, prev_raw)


def test_build_ratio_unpacks_tuple():
    """build_ratio 契约：raw/hfq 都是 (date, close) 元组 -> ratio=hc/rc。"""
    raw_last = {"600000": ("2026-09-17", 10.0)}
    hfq_last = {"600000": ("2026-09-17", 25.0)}
    assert SI.build_ratio(raw_last, hfq_last) == {"600000": 2.5}
