"""三域 payload 适配器测试（方案 §6 + 差异核对报告 D-1/D-2）。

重点验证的是**方案漏掉的东西**：
  - 黑盒线（`__DATA_BB__`）必须有自己的 variant 与路由
  - ETF 的 sector/hs300 必须拆成独立 variant（否则无法单独判新鲜度）
  - stocks.json 是 `[code, name]` 二元组列表这一形态
"""

from __future__ import annotations

import json
import os

import pytest

from common.payload.adapters import (
    ROUTES,
    SCHEMA_VERSION,
    DomainPayload,
    normalize_etf,
    normalize_quant_lab,
    normalize_stock,
    route_table,
    slug,
    write_envelopes,
)


# ---------------------------------------------------------------------------
# ★ 黑盒线：方案漏列，必须独立存在
# ---------------------------------------------------------------------------
def test_blackbox_variant_exists():
    variants = {(d, v) for d, v, _ in ROUTES}
    assert ("quant-lab", "blackbox") in variants, (
        "★ 方案漏列的黑盒线（__DATA_BB__）必须有自己的路由，"
        "否则合并站点里黑盒页永远空白")


def test_quant_lab_yields_two_payloads():
    modes = {"y3": {"on": {"equity": [1]}, "off": {"equity": [2]}}}
    modes_bb = {"y3": {"on": {"equity": [3]}, "off": {"equity": [4]}}}
    out = normalize_quant_lab(modes=modes, modes_bb=modes_bb, data_date="2026-09-11")
    assert len(out) == 2
    by_variant = {p.variant: p for p in out}
    assert set(by_variant) == {"momentum", "blackbox"}
    assert by_variant["momentum"].payload is modes
    assert by_variant["blackbox"].payload is modes_bb


def test_quant_lab_missing_blackbox_warns_not_crashes():
    """黑盒产物缺失时必须能降级（不能整站构建失败），但要明确告警。"""
    out = normalize_quant_lab(modes={"y3": {}}, modes_bb=None)
    bb = [p for p in out if p.variant == "blackbox"][0]
    assert bb.payload == {}
    assert any("黑盒" in w for w in bb.warnings)


def test_quant_lab_missing_momentum_warns():
    out = normalize_quant_lab(modes=None, modes_bb={"y3": {}})
    m = [p for p in out if p.variant == "momentum"][0]
    assert any("动量" in w for w in m.warnings)


# ---------------------------------------------------------------------------
# ETF：sector / hs300 拆分
# ---------------------------------------------------------------------------
def test_etf_splits_three_variants():
    payload = {
        "snapshot": {"date": "2026-09-11"},
        "backtest": {"metrics": {}},
        "sector": {"industries": []},
        "hs300": {"timing": []},
    }
    out = normalize_etf(payload, data_date="2026-09-11")
    assert {p.variant for p in out} == {"dividend", "sector", "hs300"}


def test_etf_dividend_keeps_only_snapshot_and_backtest():
    payload = {"snapshot": {"a": 1}, "backtest": {"b": 2},
               "sector": {"c": 3}, "hs300": {"d": 4}}
    div = [p for p in normalize_etf(payload) if p.variant == "dividend"][0]
    assert set(div.payload) == {"snapshot", "backtest"}
    assert "sector" not in div.payload, "sector 必须拆出去，否则会被重复路由"


def test_etf_optional_segments_absent_ok():
    """sector/hs300 是可选段（carry-forward 机制下可能没有）。"""
    out = normalize_etf({"snapshot": {}, "backtest": {}})
    assert {p.variant for p in out} == {"dividend"}


def test_etf_empty_payload_ok():
    assert normalize_etf(None)[0].variant == "dividend"


# ---------------------------------------------------------------------------
# 个性化选股：stocks 是 [code, name] 二元组列表
# ---------------------------------------------------------------------------
def test_stock_screen_single_variant():
    out = normalize_stock(stocks=[["000001", "平安银行"]], factors={})
    assert len(out) == 1
    assert out[0].variant == "screen"
    assert out[0].payload["stocks"] == [["000001", "平安银行"]]


def test_stock_warns_on_empty_factors():
    """实测 factors.json 当前为 {} —— 必须告警而不是静默。"""
    out = normalize_stock(stocks=[["000001", "X"]], factors={})
    assert any("因子" in w for w in out[0].warnings)


def test_stock_warns_on_malformed_entries():
    out = normalize_stock(stocks=[["000001", "X"], "bad", ["000002"]], factors={"a": 1})
    assert any("格式异常" in w for w in out[0].warnings)


def test_stock_empty_universe_warns():
    out = normalize_stock(stocks=[], factors={"a": 1})
    assert any("为空" in w for w in out[0].warnings)


def test_stock_accepts_tuple_entries():
    out = normalize_stock(stocks=[("000001", "平安银行")], factors={"x": 1})
    assert not out[0].warnings


# ---------------------------------------------------------------------------
# 信封与落盘
# ---------------------------------------------------------------------------
def test_envelope_schema():
    p = DomainPayload("etf", "dividend", {"k": 1}, data_date="2026-09-11")
    e = p.to_envelope()
    assert e["domain"] == "etf"
    assert e["variant"] == "dividend"
    assert e["schema_version"] == SCHEMA_VERSION
    assert e["data_date"] == "2026-09-11"
    assert e["payload"] == {"k": 1}
    assert "generated_at" in e


def test_write_envelopes(tmp_path):
    ps = normalize_quant_lab(modes={"y3": {}}, modes_bb={"y3": {}},
                             data_date="2026-09-11")
    paths = write_envelopes(ps, str(tmp_path))
    assert len(paths) == 2
    names = sorted(os.path.basename(x) for x in paths)
    assert names == ["quant-lab.blackbox.json", "quant-lab.momentum.json"]
    d = json.load(open(paths[0], encoding="utf-8"))
    assert d["schema_version"] == SCHEMA_VERSION


def test_route_table_shapes():
    rt = route_table()
    assert len(rt) == len(ROUTES) == 6
    for r in rt:
        assert set(r) == {"domain", "variant", "title"}


def test_slugs_unique():
    s = [slug(d, v) for d, v, _ in ROUTES]
    assert len(set(s)) == len(s)


def test_every_route_domain_has_adapter():
    from common.payload.adapters import ADAPTERS
    for d, _, _ in ROUTES:
        assert d in ADAPTERS, f"{d} 缺适配器"
