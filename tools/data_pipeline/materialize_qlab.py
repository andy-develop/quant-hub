#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据仓 → quant-lab 布局物化器（阶段E-2，2026-09-13）。

方案 §4.2「strategy-pm 读数据仓 → 跑三域引擎」的落地：quant-lab 的 signals.py 硬编码读
{BASE}/data/kline/{hfq,raw}_*.parquet + {BASE}/data/meta/{stock_basic,st_history,
index_daily,bench_daily}.parquet。数据仓统一管理后，这些文件不再由 quant-lab 自身
维护，而是由本物化器从数据仓（market/ + meta/）**确定性重建**。

物化输出与 legacy quant-lab 布局逐位一致（阶段E-2 实测：两仓重叠区间 open/close/
volume/amount max diff = 0.0；bench_daily == tencent/sh000300 逐位一致）—— 这是
shadow_diff（§7.3）逐位比对的前提：同一天、同一引擎，仅数据源不同时 payload 必须一致。

物化映射（数据仓 → quant-lab 布局）：
  market/stock/hfq             → data/kline/hfq_*.parquet    （code 归一为 1./0. secid）
  market/stock/raw             → data/kline/raw_*.parquet
  meta/universe.parquet        → data/meta/stock_basic.parquet（code 转 sh./sz. 前缀，
                                  secid 保留，ipo/out_date → datetime64 NaT）
  meta/st_history.parquet      → data/meta/st_history.parquet （code → secid 格式）
  index/broad/sh000001/daily   → data/meta/index_daily.parquet（上证，去 code 列）
  index/tencent/sh000300/daily → data/meta/bench_daily.parquet（沪深300 基准线）
  index/tencent/sh000852/daily → data/meta/csi1000_daily.parquet（中证1000，可选）

★ 幂等 / 纯函数：每次从数据仓全量重建（先清空旧 kline 物化件再写），输入仓库 →
输出布局，无状态、可重跑。不动 quant-lab 其它文件（lgbm_model/scripts 等）。

用法（CI 内，数据仓检出在 --data-root，quant-lab 检出在 --out）：
    python -m tools.data_pipeline.materialize_qlab \
        --data-root data-repo/data --out src/quant-lab-shadow --writer "strategy-pm@shadow"
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# hfq 分片只写 OHLC（与 legacy quant-lab hfq 分片同形状 6 列；量能从 raw 取，build_indicators 语义）
HFQ_COLS = ("code", "date", "open", "high", "low", "close")
RAW_COLS = ("code", "date", "open", "high", "low", "close", "volume", "amount")
# 每分片最多容纳的股票数（全A 5552 只 → 每 kind ~12 片，体积适中）
CODES_PER_SHARD = 500
INDEX_COLS = ("date", "open", "close", "high", "low", "volume", "amount")


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


def _normalize_secid(series, pd):
    """任意代码形态 → 腾讯 secid（1.600000 / 0.000001），reader.normalize_code 幂等归一。"""
    from common.store.reader import code_to_secid, normalize_code
    return pd.Series(series).map(lambda c: code_to_secid(normalize_code(str(c))))


def _clear_kline(out_root: str, pd=None) -> int:
    """清空 quant-lab data/kline 下全部物化件（顶层分片 + incremental + fixup）。

    为什么必须清：reader.load 已把 fixup 覆盖并入物化数据；若残留 legacy 的
    incremental/fixup 分片，load_klines 会再次叠加 → 重复行/双重复权覆盖（§0.4 教训）。
    """
    kdir = os.path.join(out_root, "data", "kline")
    if not os.path.isdir(kdir):
        return 0
    n = 0
    for pat in ("hfq_*.parquet", "raw_*.parquet"):
        for p in glob.glob(os.path.join(kdir, pat)):
            os.remove(p)
            n += 1
        for sub in ("incremental", "fixup"):
            for p in glob.glob(os.path.join(kdir, sub, pat)):
                os.remove(p)
                n += 1
    return n


def materialize_kline(root: str, out_root: str, fq: str, writer: str, pd=None) -> dict:
    """stock 行情 → data/kline/{fq}_{mkt}_{nn}.parquet 平铺分片（secid 格式 code）。

    分片按市场（沪 b0 / 深 b1 / 北 b2）分组、每 CODES_PER_SHARD 只一片，命名贴近
    legacy（hfq_b0_00.parquet）。load_klines 按 {fq}_*.parquet glob 全量 concat，
    分片边界不影响结果。
    """
    pd = pd or _pd()
    from common.store.reader import code_to_secid, load, normalize_code

    df = load(asset="stock", fq=fq, root=root,
              columns=["open", "high", "low", "close", "volume", "amount"])
    df["code"] = df["code"].map(lambda c: code_to_secid(normalize_code(str(c))))
    cols = list(HFQ_COLS) if fq == "hfq" else list(RAW_COLS)
    df = df[cols].sort_values(["code", "date"]).reset_index(drop=True)

    def mkt_tag(code: str) -> str:
        return "b0" if code.startswith("1.") else ("b2" if code.startswith("2.") else "b1")

    df["_mkt"] = df["code"].map(mkt_tag)
    kdir = os.path.join(out_root, "data", "kline")
    os.makedirs(kdir, exist_ok=True)
    written = []
    for mkt in sorted(df["_mkt"].unique()):
        sub = df[df["_mkt"] == mkt].drop(columns="_mkt")
        codes = sorted(sub["code"].unique())
        for i in range(0, len(codes), CODES_PER_SHARD):
            batch = codes[i:i + CODES_PER_SHARD]
            g = sub[sub["code"].isin(batch)]
            if g.empty:
                continue
            path = os.path.join(kdir, f"{fq}_{mkt}_{i // CODES_PER_SHARD:02d}.parquet")
            tmp = path + ".tmp"
            g.to_parquet(tmp, index=False)
            os.replace(tmp, path)
            written.append(os.path.relpath(path, out_root))
    return {"fq": fq, "rows": int(len(df)), "codes": int(df["code"].nunique()), "shards": len(written)}


def _to_meta_frame(pd, df, cols):
    """统一 meta 索引帧：date → datetime64[ns]，列序固定。"""
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    return d[list(cols)]


def materialize_stock_basic(root: str, out_root: str, pd=None) -> dict:
    """meta/universe → data/meta/stock_basic.parquet（与 legacy 同形状，逐位可比）。

    signals.run_scan 只消费 secid（names 映射 / cut_map），code 列转 sh./sz. 前缀
    保持与 legacy 同形状；ipo_date/out_date 统一 datetime64[ns]（None → NaT）。
    """
    pd = pd or _pd()
    from common.store.reader import load_meta
    uni = load_meta("universe", root=root)
    d = uni.copy()
    d["secid"] = _normalize_secid(d["secid"], pd).fillna(d["secid"])

    def _symbol_dot(secid: str) -> str:
        """secid(1.600000) → 交易所带点前缀（sh.600000），与 legacy stock_basic 同格式。"""
        mkt, _, num = str(secid).partition(".")
        return ("sh." if mkt == "1" else "sz.") + num

    d["code"] = d["secid"].map(_symbol_dot)
    for c in ("ipo_date", "out_date"):
        if c in d.columns:
            d[c] = pd.to_datetime(d[c], errors="coerce")
    keep = ["code", "secid", "name", "ipo_date", "out_date", "status"]
    d = d[[c for c in keep if c in d.columns]]
    d = d.sort_values("code").reset_index(drop=True)
    mdir = os.path.join(out_root, "data", "meta")
    os.makedirs(mdir, exist_ok=True)
    path = os.path.join(mdir, "stock_basic.parquet")
    d.to_parquet(path + ".tmp", index=False)
    os.replace(path + ".tmp", path)
    return {"rows": int(len(d)), "cols": sorted(d.columns)}


def materialize_st_history(root: str, out_root: str, pd=None) -> dict:
    """meta/st_history → data/meta/st_history.parquet（code → secid 格式，短线域私有表）。

    ★ st_history 是短线域私有 meta（DOMAIN_PRIVATE_META），跨域读 = 口径污染；
    物化器在 shortterm 域语境下搬运，只写不读其它域口径。
    """
    pd = pd or _pd()
    from common.store.reader import load_meta
    st = load_meta("st_history", root=root, domain="shortterm")
    d = st.copy()
    d["code"] = _normalize_secid(d["code"], pd)
    d = _to_meta_frame(pd, d, ["code", "date", "is_st"])
    mdir = os.path.join(out_root, "data", "meta")
    os.makedirs(mdir, exist_ok=True)
    path = os.path.join(mdir, "st_history.parquet")
    d.to_parquet(path + ".tmp", index=False)
    os.replace(path + ".tmp", path)
    return {"rows": int(len(d)), "last_date": str(d["date"].max().date())}


def materialize_index_daily(root: str, out_root: str, *, code: str, dst: str, pd=None) -> dict:
    """指数日K → data/meta/{dst}.parquet（去 code 列，列序与 legacy 一致）。

    上证 sh000001 → index_daily.parquet；沪深300 sh000300 → bench_daily.parquet；
    中证1000 sh000852 → csi1000_daily.parquet（可选）。volume/amount 口径已实测与
    legacy 逐位一致。
    """
    pd = pd or _pd()
    from common.store.reader import ContractViolation
    # 指数日K是 <group>/<code>/daily/ 封存分区布局（reader.load 的 daily 分支只认平铺
    # market/index/raw/，读不了这里），直接 glob 文件系统（只读，无 _incr/fixup）。
    base = os.path.join(root, "market", "index")
    files = sorted(glob.glob(os.path.join(base, "*", code, "daily", "**", "*.parquet"),
                             recursive=True))
    if not files:
        raise FileNotFoundError(f"no index daily data for {code} under {base}")
    frames = []
    for f in files:
        df = pd.read_parquet(f)
        if "code" in df.columns:
            df = df.drop(columns=["code"])
        frames.append(df)
    idx = pd.concat(frames, ignore_index=True)
    missing = [c for c in INDEX_COLS if c not in idx.columns]
    if missing:
        raise ContractViolation(
            f"index {code} daily 物化件缺列 {missing} —— 契约不匹配")
    d = _to_meta_frame(pd, idx, INDEX_COLS)
    mdir = os.path.join(out_root, "data", "meta")
    os.makedirs(mdir, exist_ok=True)
    path = os.path.join(mdir, dst)
    d.to_parquet(path + ".tmp", index=False)
    os.replace(path + ".tmp", path)
    return {"src": code, "dst": dst, "rows": int(len(d)),
            "range": f"{d['date'].min().date()}~{d['date'].max().date()}"}


def run(*, data_root: str, out: str, writer: str = "materialize_qlab", logger=print) -> dict:
    pd = _pd()
    if not os.path.isdir(os.path.join(data_root, "market")):
        raise FileNotFoundError(f"数据仓无效（无 market/）：{data_root}")
    cleared = _clear_kline(out, pd)
    logger(f"[kline] 清空旧物化件 {cleared} 个")

    summary = {
        "pipeline": "materialize_qlab",
        "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "data_root": os.path.abspath(data_root),
        "out": os.path.abspath(out),
        "writer": writer,
        "kline": {},
        "meta": {},
    }
    for fq in ("raw", "hfq"):
        r = materialize_kline(data_root, out, fq, writer, pd)
        summary["kline"][fq] = r
        logger(f"[kline] {fq}: {r['rows']:,} 行 / {r['codes']:,} 只 -> {r['shards']} 片")

    summary["meta"]["stock_basic"] = materialize_stock_basic(data_root, out, pd)
    logger(f"[meta] stock_basic {summary['meta']['stock_basic']['rows']:,} 行")
    summary["meta"]["st_history"] = materialize_st_history(data_root, out, pd)
    logger(f"[meta] st_history {summary['meta']['st_history']['rows']:,} 行（最新 "
           f"{summary['meta']['st_history']['last_date']}）")
    summary["meta"]["index_daily"] = materialize_index_daily(
        data_root, out, code="sh000001", dst="index_daily.parquet", pd=pd)
    logger(f"[meta] index_daily {summary['meta']['index_daily']['range']}")
    summary["meta"]["bench_daily"] = materialize_index_daily(
        data_root, out, code="sh000300", dst="bench_daily.parquet", pd=pd)
    logger(f"[meta] bench_daily {summary['meta']['bench_daily']['range']}")
    try:
        summary["meta"]["csi1000_daily"] = materialize_index_daily(
            data_root, out, code="sh000852", dst="csi1000_daily.parquet", pd=pd)
        logger(f"[meta] csi1000_daily {summary['meta']['csi1000_daily']['range']}")
    except Exception as e:  # noqa: BLE001
        logger(f"[meta] csi1000_daily 跳过（build_report 会 fallback 到 bench）：{str(e)[:120]}")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="数据仓 → quant-lab 布局物化器（阶段E-2）")
    ap.add_argument("--data-root", required=True, help="数据仓根（含 market/ + meta/）")
    ap.add_argument("--out", required=True, help="quant-lab 检出根（写入其 data/ 布局）")
    ap.add_argument("--writer", default="materialize_qlab")
    args = ap.parse_args(argv)
    summary = run(data_root=args.data_root, out=args.out, writer=args.writer)
    import json
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("[✓] materialize_qlab done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
