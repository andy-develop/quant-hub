#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段E-1 回归：expire 同步 manifest + etf 重派生在 expire 之后（R17 顺序）。

复现 2026-09-13 数据仓 verify 红的两类问题：
  1. [manifest] etf_raw.json 残留被 expire 删除的旧分区条目（僵尸条目）
  2. [derived]  etf weekly/monthly 的 derived_from.sha256 与 expire 后 daily 指纹不符
锁住两个修复：
  * writer.expire_partitions 删分区后同步 manifest（_prune_manifest_removed）
  * etf_incr.run 把重派生移到 expire 之后（_derive_all），所有路径（含 skip）一致
运行：pytest tests/pipeline/test_etf_expire_derive.py -q
"""
import datetime as dt
import os
import sys

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

ROOT_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_REPO)

from common.calendar import load_calendar  # noqa: E402
from common.store.writer import (  # noqa: E402
    expire_partitions,
    read_manifest,
    seal_partition,
    write_incremental,
    write_manifest,
)
from tools.data_pipeline import csindex, etf_incr, verify as V  # noqa: E402

CODES = csindex.CSINDEX_CODES  # ("H20269","H30269","H00300","000300")


def _write_calendar(meta_dir, start="2013-07-19", end="2026-12-31"):
    os.makedirs(meta_dir, exist_ok=True)
    days = pd.bdate_range(start, end)
    p = os.path.join(meta_dir, "trade_calendar.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("trade_date\n")
        for d in days:
            f.write(d.strftime("%Y-%m-%d") + "\n")
    return p


def _mk_daily(code, start, end):
    """合成契约 daily 帧（含 2013 老历史 + 2026 近期，模拟首灌全史）。"""
    days = pd.bdate_range(start, end)
    n = len(days)
    base = 3000.0 + (sum(map(ord, code)) % 1000)
    close = base * (1 + pd.Series(range(n)).astype("float64") * 0.0005)
    return pd.DataFrame({
        "code": code, "date": pd.to_datetime(days.date),
        "open": close * 0.999, "high": close * 1.001,
        "low": close * 0.999, "close": close,
        "volume": 1_000_000, "amount": close * 1_000_000 * 25.0,
    })


def _firstload_month_partitions(root, df_all):
    """模拟 csindex 首载：全 code 合并后按月 write_incremental + seal + prune_manifest。"""
    for (y, m), g in df_all.groupby([df_all["date"].dt.year, df_all["date"].dt.month]):
        first = g["date"].min().strftime("%Y%m%d")
        write_incremental(g, "etf", "raw", first, root=root, writer="test",
                          allow_overwrite=True, pd=pd)
        seal_partition("etf", "raw", int(y), int(m), root=root, writer="test", pd=pd)
    from tools.data_pipeline import prune_manifest
    prune_manifest(root, "etf", "raw")


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("etfexp"))
    _write_calendar(os.path.join(root, "meta"))
    frames = [_mk_daily(code, "2013-07-19", "2026-09-11") for code in CODES]
    _firstload_month_partitions(root, pd.concat(frames, ignore_index=True))
    cal = load_calendar(root=root)
    return root, cal


def test_expire_prunes_manifest(seeded):
    """fix1：expire 删掉旧分区后 manifest 条目同步移除（否则 verify 僵尸条目判红）。"""
    root, _ = seeded
    man0 = read_manifest(root, "etf", "raw")
    old = [p["path"] for p in man0["partitions"] if "year=2013/month=07" in p["path"]]
    assert old, "前置：2013-07 分区应在 manifest 中"

    res = expire_partitions("etf", "raw", 2430, root=root,
                            today=dt.date(2026, 9, 11), dry_run=False, pd=pd)
    assert any("year=2013" in r["dir"] for r in res["removed"]), "2013 分区应被过期删除"

    man1 = read_manifest(root, "etf", "raw")
    assert not any("year=2013/month=07" in p["path"] for p in man1["partitions"]), \
        "manifest 僵尸条目必须被同步移除"


def test_expire_prunes_historical_zombies(tmp_path):
    """对账清理：窗口内无删除时，历史遗留的僵尸 manifest 条目也被一并清除。"""
    root = str(tmp_path / "data")
    _write_calendar(os.path.join(root, "meta"))
    df = pd.concat([_mk_daily(c, "2026-01-01", "2026-09-11") for c in CODES], ignore_index=True)
    _firstload_month_partitions(root, df)
    man = read_manifest(root, "etf", "raw")
    man["partitions"].append({"path": "market/etf/raw/year=1999/month=01"})
    write_manifest(root, "etf", "raw", man)

    expire_partitions("etf", "raw", 2430, root=root,
                      today=dt.date(2026, 9, 11), dry_run=False, pd=pd)
    man1 = read_manifest(root, "etf", "raw")
    assert not any("year=1999" in p["path"] for p in man1["partitions"])


def test_derive_after_expire_consistent(seeded):
    """fix2：expire 之后重派生 -> derived_from.sha256 与当前 daily 指纹一致（R17 不再红）。"""
    root, cal = seeded
    # 复刻 etf_incr 新顺序：先 expire，后 _derive_all
    expire_partitions("etf", "raw", 2430, root=root,
                      today=dt.date(2026, 9, 11), dry_run=False, pd=pd)
    etf_incr._derive_all(CODES, root, cal, dt.date(2026, 9, 11), "test", pd)

    problems = []
    V.check_derived(root, pd, problems)
    assert problems == [], f"R17 派生一致性必须通过，问题: {problems[:5]}"


def test_expire_dry_run_keeps_manifest(tmp_path):
    """dry_run 不删分区也不动 manifest（幂等/预览语义不被破坏）。"""
    root = str(tmp_path / "data")
    _write_calendar(os.path.join(root, "meta"))
    df = pd.concat([_mk_daily(c, "2013-07-19", "2026-09-11") for c in CODES], ignore_index=True)
    _firstload_month_partitions(root, df)
    man0 = read_manifest(root, "etf", "raw")
    n0 = len(man0.get("partitions", []))
    res = expire_partitions("etf", "raw", 2430, root=root,
                            today=dt.date(2026, 9, 11), dry_run=True, pd=pd)
    assert res["dry_run"] is True and res["removed"]
    man1 = read_manifest(root, "etf", "raw")
    assert len(man1.get("partitions", [])) == n0
    # 旧分区仍在磁盘
    assert os.path.isdir(os.path.join(root, "market", "etf", "raw", "year=2013", "month=07"))
