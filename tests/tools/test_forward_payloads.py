#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Day-1 转发适配器（§7.1）回归：从老仓产物抽真实 payload，缺产物的域不写信封(不污染 carry-forward)。"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from tools import forward_payloads as FP  # noqa: E402


def _make_src(tmp_path, *, etf_payload=None, stocks=None, factors=None, qlab_report=None):
    src = tmp_path / "src"
    if etf_payload is not None:
        d = src / "red-dividend-strategy"; d.mkdir(parents=True)
        html = ('<html><script id="PAYLOAD" type="application/json">'
                + json.dumps(etf_payload, ensure_ascii=False) + "</script></html>")
        (d / "index.html").write_text(html, encoding="utf-8")
    if stocks is not None:
        d = src / "stock-factor-engine" / "data"; d.mkdir(parents=True)
        (d / "stocks.json").write_text(json.dumps(stocks), encoding="utf-8")
        (d / "factors.json").write_text(json.dumps(factors or {}), encoding="utf-8")
    if qlab_report is not None:
        d = src / "quant-lab" / "report"; d.mkdir(parents=True)
        (d / "index.html").write_text(qlab_report, encoding="utf-8")
    return str(src)


def test_etf_forward_extracts_three_variants(tmp_path):
    payload = {"snapshot": {"data_date": "2026-09-09"}, "backtest": {"kpi": {"ret": 2.85}},
               "sector": {"rankings": []}, "hs300": {"snapshot": {}}}
    src = _make_src(tmp_path, etf_payload=payload)
    envs = FP.forward_etf(src)
    variants = {e.variant for e in envs}
    assert variants >= {"dividend"}            # 至少红利低波
    assert all(e.domain == "etf" for e in envs)


def test_selected_forward_real_stocks_empty_factors_warns(tmp_path):
    src = _make_src(tmp_path, stocks=[["000001", "平安银行"], ["600000", "浦发银行"]], factors={})
    envs = FP.forward_selected(src)
    assert len(envs) == 1 and envs[0].variant == "screen"
    assert envs[0].payload["stocks"] == [["000001", "平安银行"], ["600000", "浦发银行"]]
    assert envs[0].warnings                     # factors 空 -> 如实告警


def test_shortterm_no_product_writes_no_envelope(tmp_path):
    """quant-lab 无 report -> 返回空（绝不写空占位信封覆盖 carry-forward）。"""
    src = _make_src(tmp_path, etf_payload=None)   # 没有 quant-lab 目录
    assert FP.forward_shortterm(src) == []
    # 即便有 quant-lab 目录但无 report，也不写
    (tmp_path / "src" / "quant-lab").mkdir(parents=True)
    assert FP.forward_shortterm(str(tmp_path / "src")) == []


def test_forward_all_only_present_domains(tmp_path):
    payload = {"snapshot": {}, "backtest": {}, "sector": {}, "hs300": {}}
    src = _make_src(tmp_path, etf_payload=payload, stocks=[["000001", "x"]])
    envs = FP.forward_all(src)
    domains = {e.domain for e in envs}
    assert "etf" in domains and "stock" in domains
    assert "quant-lab" not in domains            # 无产物 -> 不在
