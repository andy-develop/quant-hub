#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_payloads 自产编排回归：selected 域可自产（stocks 真实、factors 空时如实告警）。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tools import build_payloads as BP  # noqa: E402
from common.payload.adapters import write_envelopes  # noqa: E402


def test_selected_payload_produced_with_honest_warning():
    payloads = BP.build_selected(ROOT)
    assert len(payloads) == 1
    p = payloads[0]
    assert p.domain == "stock" and p.variant == "screen"
    env = p.to_envelope()
    stocks = env["payload"]["stocks"]
    assert isinstance(stocks, list) and len(stocks) > 1000, "stocks.json 应是真实 5180 只"
    # factors 当前为空 -> normalize_stock 如实告警（§9.1：不伪造）
    assert any("factors" in w or "因子" in w for w in env.get("warnings", [])), \
        f"factors 为空应告警，实际 warnings={env.get('warnings')}"


def test_write_envelopes_roundtrip(tmp_path):
    payloads = BP.build(ROOT)
    paths = write_envelopes(payloads, str(tmp_path))
    assert paths
    obj = json.load(open(paths[0], encoding="utf-8"))
    assert obj["domain"] == "stock" and obj["variant"] == "screen"
    assert obj["schema_version"]


def test_no_stock_data_yields_empty(tmp_path):
    # 空 repo_root（无 domains/selected/data）-> 不产，交给 carry-forward
    assert BP.build_selected(str(tmp_path)) == []
