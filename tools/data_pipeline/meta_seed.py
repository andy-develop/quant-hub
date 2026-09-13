#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据仓 meta 慢变量种子化（阶段E-1，2026-09-13）。

数据仓 README 文档化了 meta/{universe,st_history,...}，但首灌时只种了
trade_calendar.csv。缺 universe 的后果（阶段B 幂等跳过掩盖了）：
data-stock-incr 下一次真实运行的 load_meta("universe") 会直接 StoreError 崩 ——
日级增量断链。本脚本从 quant-lab 现存 meta（stock_basic.parquet / st_history.parquet）
**1:1 搬运**到数据仓 meta/：不改列、不改口径、只换存放位置；幂等可重跑。

  universe.parquet   ← stock_basic.parquet   (code/secid/name/ipo_date/out_date/status)
  st_history.parquet ← st_history.parquet    (code/date/is_st，短线域私有时点表)

st_history 为短线域私有表（schema.DOMAIN_PRIVATE_META），本脚本只写入不读取，
无跨域口径问题；后续由 data-meta 周更（方案 §13.3）维持新鲜度。

用法：
    python -m tools.data_pipeline.meta_seed \
        --source-meta /path/to/quant-lab/data/meta --data-root data
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

UNIVERSE_SRC = "stock_basic.parquet"
UNIVERSE_REQUIRED = {"code", "secid", "name", "ipo_date", "out_date", "status"}
ST_SRC = "st_history.parquet"
ST_REQUIRED = {"code", "date", "is_st"}


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


def seed_universe(source_meta: str, root: str, pd) -> dict:
    src = os.path.join(source_meta, UNIVERSE_SRC)
    if not os.path.exists(src):
        raise FileNotFoundError(f"源表缺失：{src}（先检出 quant-lab 的 data/meta）")
    df = pd.read_parquet(src)
    missing = UNIVERSE_REQUIRED - set(df.columns)
    if missing:
        raise AssertionError(f"universe 源表缺列 {sorted(missing)}（{src}）")
    mdir = os.path.join(root, "meta")
    os.makedirs(mdir, exist_ok=True)
    dst = os.path.join(mdir, "universe.parquet")
    df.to_parquet(dst + ".tmp", index=False)
    os.replace(dst + ".tmp", dst)
    return {"src": src, "dst": dst, "rows": int(len(df)), "cols": sorted(df.columns)}


def seed_st_history(source_meta: str, root: str, pd) -> dict:
    src = os.path.join(source_meta, ST_SRC)
    if not os.path.exists(src):
        raise FileNotFoundError(f"源表缺失：{src}（先检出 quant-lab 的 data/meta）")
    df = pd.read_parquet(src)
    missing = ST_REQUIRED - set(df.columns)
    if missing:
        raise AssertionError(f"st_history 源表缺列 {sorted(missing)}（{src}）")
    mdir = os.path.join(root, "meta")
    os.makedirs(mdir, exist_ok=True)
    dst = os.path.join(mdir, "st_history.parquet")
    df.to_parquet(dst + ".tmp", index=False)
    os.replace(dst + ".tmp", dst)
    last = pd.to_datetime(df["date"]).max().date().isoformat()
    return {"src": src, "dst": dst, "rows": int(len(df)), "last_date": last}


def run(*, source_meta: str, root: str = "data", logger=print) -> dict:
    pd = _pd()
    summary = {
        "pipeline": "meta_seed",
        "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "source_meta": source_meta,
        "tables": {},
    }
    summary["tables"]["universe"] = seed_universe(source_meta, root, pd)
    logger(f"[seed] universe {summary['tables']['universe']['rows']:,} 行 -> "
           f"{os.path.relpath(summary['tables']['universe']['dst'], root)}")
    summary["tables"]["st_history"] = seed_st_history(source_meta, root, pd)
    s = summary["tables"]["st_history"]
    logger(f"[seed] st_history {s['rows']:,} 行（最新 {s['last_date']}）-> "
           f"{os.path.relpath(s['dst'], root)}")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="数据仓 meta 慢变量种子化（universe / st_history）")
    ap.add_argument("--source-meta", required=True,
                    help="quant-lab data/meta 目录（stock_basic.parquet / st_history.parquet 所在）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    args = ap.parse_args(argv)
    summary = run(source_meta=args.source_meta, root=args.data_root)
    print("[✓] meta_seed done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
