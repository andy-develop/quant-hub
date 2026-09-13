#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个股缺口回补（阶段E-2 收尾，2026-09-13）：legacy 分片里 <cutoff 的 hfq/raw 灌入数据仓。

背景（Q1 用户决策，2026-09-13）：
  数据仓 stock 730 交易日保留期（Q5 日级增量顺手删）把回测起点 2023-09-01 所需的
  hfq 预热窗口（ret120 指标需 2023-01 起的数据）删掉了 → 数据仓重建的 quant-lab 布局
  在 2023-09 初无信号 vs legacy 有信号 → shadow_diff（§7.3）永远无法逐位晋级。
  用户决策：回补 2023-01..08 + stock 保留期 730→1250 交易日（5 年）。

本脚本从 legacy quant-lab data/kline 提取 <cutoff 的窗口（E-2a 已实测与数据仓重叠区间
逐位一致，零网络成本、口径可信），按 stock_migrate 同款契约（raw: amount=源amount>0?
源amount:close*volume*100 / volume=round(amount/close)；hfq: 价用 hfq、量额从 raw 并入）
落盘 market/stock/{raw,hfq}/year=YYYY/month=MM/ 分区。

★ 幂等：write_sealed 只写 <cutoff 的月份（2023-01..08 当前不存在，无覆盖冲突）；
  只允许一次性回补缺口，不重写已存在分区（writer 铁律：分区内重复 (code,date) 抛错）。

用法：
    python -m tools.data_pipeline.stock_backfill \
        --kline /path/to/quant-lab/data/kline --data-root data \
        --writer "stock-backfill@2023-01..08"
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ASSET = "stock"
DEFAULT_CUTOFF = "2023-09-01"   # 只回补此日之前的缺口窗口（含 2023-08-31 最后一交易日）


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


def run(kline_dir: str, root: str, writer: str, *, cutoff: str = DEFAULT_CUTOFF,
        logger=print) -> dict:
    pd = _pd()
    from tools.data_pipeline.stock_migrate import (
        attach_real_vol_amount,
        read_kline_dir,
        to_contract_raw,
        write_sealed,
    )

    cut = pd.Timestamp(cutoff)
    summary = {"pipeline": "stock_backfill", "cutoff": cutoff, "writer": writer,
               "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
               .isoformat(timespec="seconds"), "raw": {}, "hfq": {}}

    raw_src = read_kline_dir(kline_dir, "raw", pd)
    raw_gap = raw_src[raw_src["date"] < cut]
    logger(f"[backfill] raw 全量 {len(raw_src):,} 行，缺口窗口 <{cutoff} = {len(raw_gap):,} 行")
    if raw_gap.empty:
        logger("[backfill] raw 无缺口，跳过")
    else:
        raw_ct = to_contract_raw(raw_gap, pd)
        months = write_sealed(raw_ct, "raw", root, writer, pd)
        summary["raw"] = {"rows": int(len(raw_ct)), "months": months,
                          "range": f"{raw_gap['date'].min().date()}~{raw_gap['date'].max().date()}"}
        logger(f"[backfill] raw 落盘 {len(raw_ct):,} 行 -> {months} 月分区")

    hfq_src = read_kline_dir(kline_dir, "hfq", pd)
    hfq_gap = hfq_src[hfq_src["date"] < cut]
    logger(f"[backfill] hfq 全量 {len(hfq_src):,} 行，缺口窗口 <{cutoff} = {len(hfq_gap):,} 行")
    if hfq_gap.empty:
        logger("[backfill] hfq 无缺口，跳过")
    else:
        # 量额从 raw 契约帧并入（hfq 自身 volume/amount 与复权无关，同窗口 raw 已落盘/待落盘）
        raw_for_attach = to_contract_raw(raw_gap, pd)
        hfq_ct = attach_real_vol_amount(hfq_gap, raw_for_attach, pd)
        months = write_sealed(hfq_ct, "hfq", root, writer, pd)
        summary["hfq"] = {"rows": int(len(hfq_ct)), "months": months,
                          "range": f"{hfq_gap['date'].min().date()}~{hfq_gap['date'].max().date()}"}
        logger(f"[backfill] hfq 落盘 {len(hfq_ct):,} 行 -> {months} 月分区")

    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="个股缺口回补（legacy 分片 <cutoff 灌入数据仓）")
    ap.add_argument("--kline", required=True, help="quant-lab/data/kline 目录（legacy 数据源）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--writer", default="stock-backfill@2023-01..08")
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="回补截止（不含此日）")
    args = ap.parse_args(argv)
    res = run(args.kline, args.data_root, args.writer, cutoff=args.cutoff)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("[✓] 缺口回补完成。下一步：跑 tools/data_pipeline/verify.py + 物化器重跑 shadow_diff")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
