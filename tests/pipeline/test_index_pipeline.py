#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指数抓取编排（tools/data_pipeline/index.py）的离线回归测试。

不联网：用合成 bars 跑完整链路（daily 封存 -> weekly/monthly 派生 -> reader 回读），
锁住方案 §2.7 的正确性前提：
  * daily 是唯一权威、amount 为 NaN 不填 0
  * weekly/monthly 物化且带 is_partial；load 默认 closed_only=True 剔除未完成周期
    （ETF 域实测：不剔除未完成 ISO 周 -> 97.7% 周中日期信号错位）
  * 两个大盘必须同时在场，缺任一 -> 新鲜度门禁红
  * 派生 manifest 带 derived_from.sha256，可被 assert_derived_consistent 校验
运行：pytest tests/pipeline -q
"""
import datetime as dt
import json
import os
import sys

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

ROOT_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_REPO)

from tools.data_pipeline import index as IDX  # noqa: E402
from common.store.reader import load  # noqa: E402
from common.calendar import load_calendar  # noqa: E402
from common.aggregate import assert_derived_consistent  # noqa: E402


def _write_calendar(meta_dir):
    """造一个覆盖合成区间的交易日历（工作日近似），单列 trade_date。"""
    os.makedirs(meta_dir, exist_ok=True)
    days = pd.bdate_range("2026-01-01", "2026-12-31")
    p = os.path.join(meta_dir, "trade_calendar.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("trade_date\n")
        for d in days:
            f.write(d.strftime("%Y-%m-%d") + "\n")
    return p


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("idxdata"))
    _write_calendar(os.path.join(root, "meta"))
    asof = dt.date(2026, 9, 11)          # 周五：该 ISO 周已走完，9 月未走完
    summary = IDX.run(IDX.BROAD, root=root, asof=asof, writer="test", offline=True)
    cal = load_calendar(root=root)
    return root, asof, summary, cal


def test_pipeline_runs_and_seals(prepared):
    root, asof, summary, cal = prepared
    assert summary["daily_rows"] > 0
    assert summary["months_sealed"] >= 3
    assert summary["freshness"]["level"] == "green"


def test_daily_is_authoritative_amount_nan(prepared):
    root, *_ = prepared
    d = load(asset="index", fq="raw", freq="daily", code="sh000001", root=root)
    assert len(d) > 0
    assert d["amount"].isna().all(), "指数 amount 必须为 NaN，不得填 0（§2.7）"
    assert str(d["date"].max().date()) == "2026-09-11"


def test_both_broad_indices_present_and_distinct(prepared):
    root, *_ = prepared
    a = load(asset="index", fq="raw", freq="daily", code="sh000001", root=root)
    b = load(asset="index", fq="raw", freq="daily", code="sz399001", root=root)
    assert len(a) > 0 and len(b) > 0
    assert a["code"].iloc[0] != b["code"].iloc[0]


def test_weekly_has_is_partial_and_closed(prepared):
    root, *_ = prepared
    w = load(asset="index", fq="raw", freq="weekly", code="sh000001", root=root)
    assert "is_partial" in w.columns
    # asof=周五 -> 所有已物化周都走完（合成数据止于 09-11）
    assert not bool(w["is_partial"].any())


def test_monthly_closed_only_drops_incomplete_september(prepared):
    root, *_ = prepared
    m_closed = load(asset="index", fq="raw", freq="monthly", code="sh000001", root=root)
    m_all = load(asset="index", fq="raw", freq="monthly", code="sh000001",
                 root=root, closed_only=False)
    # 9 月未走完（asof=09-11，日历里 9 月还有更晚交易日）-> closed_only 必须剔除它
    assert len(m_all) - len(m_closed) == 1
    assert not bool(m_closed["is_partial"].any())
    assert bool(m_all["is_partial"].any())


def test_derived_manifest_consistency(prepared):
    root, asof, summary, cal = prepared
    mp = os.path.join(root, "manifest", "index_broad_sh000001_weekly.json")
    assert os.path.exists(mp)
    man = json.load(open(mp, encoding="utf-8"))
    assert man["derived"] is True
    assert man["rule"].startswith("ISO-week")
    daily = load(asset="index", fq="raw", freq="daily", code="sh000001", root=root)
    fp = IDX.daily_fingerprint(daily)
    # 一致：不抛
    assert_derived_consistent(man, fp)
    # daily 变了（指纹不符）：必须抛（R17 派生滞后）
    with pytest.raises(AssertionError):
        assert_derived_consistent(man, "deadbeef" * 8)


def test_freshness_gate_red_when_index_missing(prepared):
    root, asof, summary, cal = prepared
    d = load(asset="index", fq="raw", freq="daily", code="sh000001", root=root)
    # 只有上证、缺深成 -> 红（§2.7：不允许半套大盘入库）
    level, gate = IDX.freshness_gate({"sh000001": d}, cal, asof)
    assert level == "red"
    assert any("缺失" in p for p in gate["problems"])


def test_freshness_gate_red_when_stale(prepared):
    root, asof, summary, cal = prepared
    d = load(asset="index", fq="raw", freq="daily", code="sh000001", root=root)
    # asof 推到数据之后很多 -> 最近交易日 > date_max -> 红
    future = dt.date(2026, 12, 31)
    level, gate = IDX.freshness_gate({"sh000001": d, "sz399001": d}, cal, future)
    assert level == "red"
