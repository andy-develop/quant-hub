#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shadow_diff（方案 §7.3）回归：逐位一致才 PROMOTE-READY，任何差异 BLOCKED 并给出根因提示。"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tools import shadow_diff as SD  # noqa: E402


def _env(dom, var, payload, data_date="2026-09-11", gen="2026-09-11T17:00:00+08:00"):
    return {"domain": dom, "variant": var, "payload": payload,
            "data_date": data_date, "generated_at": gen, "schema_version": "1"}


def _write(d, envs):
    os.makedirs(d, exist_ok=True)
    for e in envs:
        with open(os.path.join(d, f"{e['domain']}.{e['variant']}.json"), "w", encoding="utf-8") as f:
            json.dump(e, f, ensure_ascii=False)


def test_identical_promotes_ready(tmp_path):
    p = {"kpi": {"ret": 2.853, "sharpe": 0.90, "mdd": -0.29, "trades": 44}, "table": [1, 2, 3]}
    sh = tmp_path / "sh"; lg = tmp_path / "lg"
    _write(str(sh), [_env("etf", "dividend", p)])
    _write(str(lg), [_env("etf", "dividend", p)])
    res = SD.diff(str(sh), str(lg))
    assert res["by_domain"]["etf"]["verdict"] == "PROMOTE-READY"


def test_generated_at_difference_is_ignored(tmp_path):
    p = {"kpi": {"ret": 1.0, "sharpe": 0.5}}
    sh = tmp_path / "sh"; lg = tmp_path / "lg"
    _write(str(sh), [_env("etf", "dividend", p, gen="2026-09-11T17:00:00+08:00")])
    _write(str(lg), [_env("etf", "dividend", p, gen="2026-09-12T08:00:00+08:00")])
    res = SD.diff(str(sh), str(lg))
    # generated_at 是 volatile，剔除后应逐位一致
    assert res["by_domain"]["etf"]["verdict"] == "PROMOTE-READY"


def test_kpi_delta_blocks(tmp_path):
    sh = tmp_path / "sh"; lg = tmp_path / "lg"
    _write(str(sh), [_env("etf", "dividend", {"kpi": {"ret": 2.853, "sharpe": 0.90}})])
    _write(str(lg), [_env("etf", "dividend", {"kpi": {"ret": 2.854, "sharpe": 0.90}})])
    res = SD.diff(str(sh), str(lg), tol=1e-9)
    v = res["by_domain"]["etf"]
    assert v["verdict"] == "BLOCKED"
    assert v["max_kpi_delta"] == pytest.approx(0.001, abs=1e-6)
    assert "复权" in v["hint"] or "KPI" in v["hint"]


def test_data_date_mismatch_blocks(tmp_path):
    p = {"kpi": {"ret": 1.0}}
    sh = tmp_path / "sh"; lg = tmp_path / "lg"
    _write(str(sh), [_env("etf", "dividend", p, data_date="2026-09-11")])
    _write(str(lg), [_env("etf", "dividend", p, data_date="2026-09-10")])
    res = SD.diff(str(sh), str(lg))
    v = res["by_domain"]["etf"]
    assert v["verdict"] == "BLOCKED"
    assert "交易日边界" in v["hint"]


def test_missing_kpi_field_blocks(tmp_path):
    sh = tmp_path / "sh"; lg = tmp_path / "lg"
    _write(str(sh), [_env("etf", "dividend", {"kpi": {"ret": 1.0, "sharpe": 0.5}})])
    _write(str(lg), [_env("etf", "dividend", {"kpi": {"ret": 1.0}})])
    res = SD.diff(str(sh), str(lg))
    assert res["by_domain"]["etf"]["verdict"] == "BLOCKED"


def test_not_started_and_domain_rollup(tmp_path):
    sh = tmp_path / "sh"; lg = tmp_path / "lg"
    # legacy 有、shadow 没有 -> NOT-STARTED
    _write(str(sh), [])
    _write(str(lg), [_env("shortterm", "momentum", {"kpi": {"ret": 1.0}})])
    res = SD.diff(str(sh), str(lg))
    assert res["by_domain"]["shortterm"]["verdict"] == "NOT-STARTED"


def test_domain_rollup_takes_worst(tmp_path):
    sh = tmp_path / "sh"; lg = tmp_path / "lg"
    good = {"kpi": {"ret": 1.0}}
    _write(str(sh), [_env("etf", "dividend", good), _env("etf", "sector", {"kpi": {"ret": 2.0}})])
    _write(str(lg), [_env("etf", "dividend", good), _env("etf", "sector", {"kpi": {"ret": 2.5}})])
    res = SD.diff(str(sh), str(lg))
    # 一个 variant READY、一个 BLOCKED -> 域取最差 BLOCKED
    assert res["by_domain"]["etf"]["verdict"] == "BLOCKED"


def test_strict_exit_code(tmp_path):
    sh = tmp_path / "sh"; lg = tmp_path / "lg"
    _write(str(sh), [_env("etf", "dividend", {"kpi": {"ret": 1.0}})])
    _write(str(lg), [_env("etf", "dividend", {"kpi": {"ret": 9.9}})])
    out = tmp_path / "sd.json"
    rc = SD.main(["--shadow", str(sh), "--legacy", str(lg), "--out", str(out), "--strict"])
    assert rc == 1
    assert json.loads(out.read_text(encoding="utf-8"))["by_domain"]["etf"]["verdict"] == "BLOCKED"
