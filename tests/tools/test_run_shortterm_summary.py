#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_shortterm 回测摘要（Q3：摘要入 state/）单元测试。

★ 关键契约：_build_summary 的 KPI 口径必须与 tools/shadow_diff.extract_kpis
逐位一致（阶段 F shadow_diff 比对的输入就是这两条链路）。
"""
from __future__ import annotations

import json

import pytest

from tools.run_shortterm import _build_summary, _write_summary


def _line(equity, pnls=None):
    return {
        "equity": equity,
        "trades": [{"pnl_pct": p} for p in (pnls or [])],
    }


def _env(domain, variant, y3):
    return {"domain": domain, "variant": variant, "payload": {"y3": y3}}


def _sample_payloads():
    mom_on = _line([1.0, 1.2, 0.9, 1.15], [0.2, -0.1, 0.05])
    mom_off = _line([1.0, 0.8, 1.05], [-0.2, 0.3])
    bb_on = _line([1.0, 1.3, 1.1], [0.3, -0.05])
    bb_off = _line([1.0, 1.0], [])
    return [
        _env("quant-lab", "momentum", {"on": mom_on, "off": mom_off}),
        _env("quant-lab", "blackbox", {"on": bb_on, "off": bb_off}),
    ]


def test_build_summary_matches_shadow_diff_kpis():
    """摘要 24 项 KPI 与 shadow_diff.extract_kpis 逐位一致（口径冻结）。"""
    from tools.shadow_diff import extract_kpis

    payloads = _sample_payloads()
    summary = _build_summary(payloads, data_date="2026-09-11", has_blackbox=True,
                             writer="test")
    assert summary["domain"] == "shortterm"
    assert summary["data_date"] == "2026-09-11"
    assert summary["has_blackbox"] is True
    assert set(summary["kpis"]) == {"momentum", "blackbox"}

    # 与 shadow_diff 权威抽取逐位一致
    ref = extract_kpis("shortterm", payloads)
    flat = []
    for variant in ("momentum", "blackbox"):
        for line in ("on", "off"):
            k = summary["kpis"][variant][line]
            flat += [k["ret"], k["mdd"], k["sharpe"], float(k["n_trades"]),
                     k["win_rate"], k["last_equity"]]
    assert flat == pytest.approx(ref, abs=1e-9)


def test_build_summary_empty_nodes_zero():
    """缺线/空数据 -> 6 项全 0（与 shadow_diff._shortterm_line_kpis 同口径）。"""
    payloads = [
        _env("quant-lab", "momentum", {"on": None, "off": {}}),
        _env("quant-lab", "blackbox", None),
    ]
    summary = _build_summary(payloads, data_date=None, has_blackbox=False)
    for variant in ("momentum", "blackbox"):
        for line in ("on", "off"):
            k = summary["kpis"][variant][line]
            assert k["ret"] == 0.0 and k["mdd"] == 0.0 and k["sharpe"] == 0.0
            assert k["n_trades"] == 0.0 and k["win_rate"] == 0.0
            assert k["last_equity"] == 0.0


def test_write_summary_idempotent(tmp_path):
    out = tmp_path / "state" / "shortterm" / "backtest_summary.json"
    s1 = _build_summary(_sample_payloads(), data_date="2026-09-11", has_blackbox=True)
    p1 = _write_summary(s1, str(out))
    s2 = _build_summary(_sample_payloads(), data_date="2026-09-11", has_blackbox=True)
    p2 = _write_summary(s2, str(out))
    assert p1 == p2 == str(out)
    assert json.loads(open(out, encoding="utf-8").read())["data_date"] == "2026-09-11"


def test_write_summary_empty_out_noop(tmp_path):
    assert _write_summary({}, "") == ""
