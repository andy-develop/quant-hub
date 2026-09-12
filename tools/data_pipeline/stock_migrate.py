#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个股数据迁移：quant-lab/data/kline -> quant-hub-data market/stock/{raw,hfq}（方案 §2.2/§2.4）。

⚠ 这是"首灌"脚本，**需口径确认后**才在 CI 跑（见文件末「确认清单」）。一次性把 quant-lab
现存 ~280MB parquet 重灌为契约分区，不是重新联网抓取。

========================= volume 单位 / amount 缺失 调查结论 =========================
quant-lab 的 kline 由三个源拼成，volume 单位**不一致**、amount **部分缺失**：

  源                  volume 单位   amount            出处
  ifzq 日K回补         手(lot)       ✗ 无              backfill_kline.py COLS 无 amount
  qt.gtimg 每日快照    手(lot)       ✓ 元(parts[37]万元×1e4)  daily_update.py:77-79
  baostock 回补        股(share)     ✓ 元              backfill_baostock.py FIELDS 含 amount

  HANDOFF 实证：「baostock volume 单位是股非手」「腾讯快照 parts[37] 是万元」。
  signals.py:85-90 的流动性口径：
      if "amount" in r.columns: amt = r["amount"].fillna(close*volume*100)
      else:                     amt = close*volume*100          # ← volume 当作"手"
  选股过滤 m5: amt20 >= 3e7（20日均额≥3000万元）。

★ 关键发现（决定迁移可行性）：
  1. signals/engine 对 **volume 只有"尺度不变"的用法**：vol_ma5/ma20、shrink=volume>=2*prev_vol_ma5
     （都是同一只股自身的比值）；engine.py 手续费按成交金额、撮合用 hfq 价，**无绝对成交量约束**。
     → 对某只股整列 volume 乘以任意常数（手→股 ×100）**不改任何信号与回测**。
  2. 唯一必须逐位保真的是 **amount(元)**。而 amount 可由 close 与 volume 反推、且与源无关：
        amount_元 = 源 amount（若存在且>0） else close_raw × volume_手 × 100
        volume_股 = round(amount_元 / close_raw)        # 对三源都得到正确的"股"数
     这恰好**逐行复现 signals.py 现在的 amt**（有 amount 用 amount、无则 close*volume*100）。

→ 迁移规则（既满足契约 volume=股/amount=元，又逐位保留短线基线）：
     raw : amount = 源amount>0 ? 源amount : close*volume*100 ;  volume = round(amount/close)
     hfq : 价用后复权；volume/amount 是真实量额（与复权无关）→ 按 (code,date) 从 raw 并入
=====================================================================================

用法（CI 内，三仓都检出后）：
    python -m tools.data_pipeline.stock_migrate \
        --kline /path/to/quant-lab/data/kline --data-root data --writer "data-stock-firstload@run N"
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ASSET = "stock"


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


def to_contract_raw(df, pd=None):
    """raw 帧 -> 契约帧：解析 amount(元) 与 volume(股)，逐行复现 signals 的 amt 口径。

    输入列：code,date,open,high,low,close,volume[,amount]（volume 单位混合：手/股）。
    输出列：code,date,open,high,low,close,volume(股,int64),amount(元,float64)。
    """
    pd = pd or _pd()
    import numpy as np
    d = df.copy()
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d["volume"] = pd.to_numeric(d["volume"], errors="coerce")
    if "amount" in d.columns:
        amt = pd.to_numeric(d["amount"], errors="coerce")
    else:
        amt = pd.Series(np.nan, index=d.index)
    # amount 缺失/<=0 -> 用 close*volume(手)*100 兜底（= signals 现有 fallback，逐行一致）
    fallback = d["close"] * d["volume"] * 100.0
    amount = amt.where(amt.notna() & (amt > 0), fallback)
    # volume_股 = amount/close（close>0 由不变量保证；为 0/NaN 的行退化为 volume*100 假设手）
    safe_close = d["close"].where(d["close"] > 0)
    vol_shares = (amount / safe_close).round()
    vol_shares = vol_shares.where(safe_close.notna(), d["volume"] * 100).fillna(0).astype("int64")
    out = d[["code", "date", "open", "high", "low", "close"]].copy()
    for c in ("open", "high", "low", "close"):
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    out["volume"] = vol_shares
    out["amount"] = amount.astype("float64")
    return out


def attach_real_vol_amount(hfq_df, raw_contract_df, pd=None):
    """hfq 帧的 volume/amount 用真实量额（与复权无关）—— 按 (code,date) 从 raw 契约帧并入。"""
    pd = pd or _pd()
    from common.store.reader import normalize_code
    h = hfq_df.copy()
    h["code"] = h["code"].map(normalize_code)
    h["date"] = pd.to_datetime(h["date"])
    # hfq 自带的 volume/amount 是错的（量额与复权无关，且 hfq close 反推会偏）——丢弃，用 raw 的真实值
    h = h.drop(columns=[c for c in ("volume", "amount") if c in h.columns])
    r = raw_contract_df.copy()
    r["code"] = r["code"].map(normalize_code)
    r["date"] = pd.to_datetime(r["date"])
    r = r[["code", "date", "volume", "amount"]]
    merged = h.merge(r, on=["code", "date"], how="left")
    # 极少数 hfq 有而 raw 无的行：amount 用 hfq_close*volume*100 兜底、volume 反推（保守）
    import numpy as np
    miss = merged["amount"].isna()
    if miss.any():
        merged.loc[miss, "amount"] = (merged.loc[miss, "close"] *
                                      pd.to_numeric(merged.loc[miss, "volume"], errors="coerce").fillna(0) * 100)
        merged.loc[miss, "volume"] = 0
    merged["volume"] = merged["volume"].fillna(0).astype("int64")
    merged["amount"] = merged["amount"].astype("float64")
    for c in ("open", "high", "low", "close"):
        merged[c] = pd.to_numeric(merged[c], errors="coerce").astype("float64")
    return merged[["code", "date", "open", "high", "low", "close", "volume", "amount"]]


def read_kline_dir(kline_dir: str, fq: str, pd=None):
    """读 quant-lab data/kline 的 base/<fq> + fixup/<fq>_* + incremental/<fq>_*，按 T-2 顺序合并。

    ★ 顺序（§9.6 T-2）：先 base，再 fixup 覆盖（fixup 是除权重算的权威值），最后 incremental。
      fixup glob 必须限定 <fq>_*（glob 到另一口径会把复权行拼进不复权库）。
      代码先 normalize 再叠加覆盖（reader._normalize_frame 同款顺序）。
    """
    pd = pd or _pd()
    from common.store.reader import normalize_code
    frames = []
    base = sorted(glob.glob(os.path.join(kline_dir, "base", fq, "*.parquet")))
    base += sorted(glob.glob(os.path.join(kline_dir, f"{fq}", "*.parquet")))    # 旧平铺命名
    fixup = sorted(glob.glob(os.path.join(kline_dir, "fixup", f"{fq}_*.parquet")))
    incr = sorted(glob.glob(os.path.join(kline_dir, "incremental", f"{fq}_*.parquet")))
    for grp in (base, fixup, incr):
        for p in grp:
            try:
                frames.append(pd.read_parquet(p))
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 跳过 {p}: {e}")
    if not frames:
        raise RuntimeError(f"{kline_dir} 下没有 {fq} 数据（base/fixup/incremental 均空）")
    df = pd.concat(frames, ignore_index=True)
    df["code"] = df["code"].map(normalize_code)
    df["date"] = pd.to_datetime(df["date"])
    # 后写的覆盖先写的（fixup/incremental 权威）：按 (code,date) 保留最后一条
    df = df.sort_values(["code", "date"], kind="stable").drop_duplicates(
        subset=["code", "date"], keep="last").reset_index(drop=True)
    return df


def write_sealed(df, fq: str, root: str, writer: str, pd=None) -> int:
    """逐月 write_incremental + seal_partition（复用指数链路同款，只用公共 API）+ prune manifest。"""
    pd = pd or _pd()
    from common.store.writer import write_incremental, seal_partition
    from tools.data_pipeline import prune_manifest
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    months = sorted(set(zip(df["date"].dt.year.tolist(), df["date"].dt.month.tolist())))
    for (y, m) in months:
        g = df[(df["date"].dt.year == y) & (df["date"].dt.month == m)]
        if g.empty:
            continue
        first = g["date"].min().strftime("%Y%m%d")
        write_incremental(g, ASSET, fq, first, root=root, writer=writer,
                          allow_overwrite=True, pd=pd)
        seal_partition(ASSET, fq, int(y), int(m), root=root, writer=writer, pd=pd)
    prune_manifest(root, ASSET, fq)
    return len(months)


def migrate(kline_dir: str, root: str, writer: str, *, fqs=("raw", "hfq"), logger=print) -> dict:
    pd = _pd()
    raw_src = read_kline_dir(kline_dir, "raw", pd)
    raw_ct = to_contract_raw(raw_src, pd)
    n_raw_months = write_sealed(raw_ct, "raw", root, writer, pd)
    logger(f"[migrate] raw: {len(raw_ct):,} 行 -> {n_raw_months} 月分区")

    out = {"raw_rows": int(len(raw_ct)), "raw_months": n_raw_months}
    if "hfq" in fqs:
        hfq_src = read_kline_dir(kline_dir, "hfq", pd)
        hfq_ct = attach_real_vol_amount(hfq_src, raw_ct, pd)
        n_hfq_months = write_sealed(hfq_ct, "hfq", root, writer, pd)
        logger(f"[migrate] hfq: {len(hfq_ct):,} 行 -> {n_hfq_months} 月分区")
        out.update({"hfq_rows": int(len(hfq_ct)), "hfq_months": n_hfq_months})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="个股数据迁移（quant-lab kline -> 契约分区）")
    ap.add_argument("--kline", required=True, help="quant-lab/data/kline 目录")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--writer", default="data-stock-firstload")
    ap.add_argument("--fq", default="raw,hfq")
    args = ap.parse_args(argv)
    fqs = tuple(x.strip() for x in args.fq.split(",") if x.strip())
    res = migrate(args.kline, args.data_root, args.writer, fqs=fqs)
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("[✓] 个股迁移完成。下一步：跑 verify + shadow_diff 对老仓基线逐位比对（见文件头确认清单）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# ============================ 确认清单（灌之前必须逐项过）============================
# [ ] 1. volume 单位结论确认：ifzq/快照=手、baostock=股；迁移用 amount/close 反推 volume_股（源无关）
# [ ] 2. amount 口径确认：amount = 源amount>0 ? 源amount : close_raw*volume_手*100（逐行复现 signals.amt）
# [ ] 3. 迁移后跑 tools.data_pipeline.verify（契约/不变量/manifest 全绿）
# [ ] 4. shadow_diff 基线：用迁移后的 data 跑 quant-lab engine.run_backtest，与"迁移前用原 data/kline
#        跑出的 KPI"逐位比对（开模式 +114.1%/-13.1%/1.44/460/42.2%；关 +52.1%/-37.4%/0.60/849/41.5%）
#        ⚠ 但 §7.2：该基线建立在 §0.4 沪市覆盖故障数据上，须先合 PR#2 + 重跑 quant-lab 重取基线，
#          再以"修复后基线"为准做 shadow_diff，否则是把 bug 固化成标准。
# [ ] 5. 逐位一致才把 shortterm 的 payload_source 由 shadow 转 primary（Phase-4，连续 3 交易日）
# ====================================================================================
