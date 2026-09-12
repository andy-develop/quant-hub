#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中证官网指数抓取编排（ETF 资产类，方案 §2.7 的 csindex 组）。

背景（为什么 ETF 资产类灌中证官网指数，而不是腾讯 ifzq）：
  schema.INDEX_SPECIAL 硬性警告 —— 全收益指数（H20269/H30269/H00300）**只有中证官网有**，
  绝不能用腾讯的价格指数替代（ETF 域 v7.12「地基修正」就是错用价格指数漏掉全部分红，
  策略收益被系统性低估 +117pp）。所以：
    * 价格指数（000300 沪深300 等）与全收益指数统一走 csindex 官网（口径唯一）
    * 全收益指数 open/high/low 官网不返回（null）-> 退化为 close（满足不变量
      low <= min(o,c) <= max(o,c) <= high；上层不得拿 OHLC 做日内因子）
    * amount 单位：官网 tradingValue 为**亿元**，契约要求**元** -> ×1e8
    * volume 单位：官网 tradingVol 为**股**（与契约一致，不换算）

运行序列（同一次运行内完成，杜绝派生滞后）：
  1. fetch   中证官网 index-perf 全史（按年分段，官网接口一次可跨多年但分段更稳）
  2. gate    新鲜度门禁：daily date_max 落后最近已收盘交易日 >0 -> 红、中止、不落盘
  3. write   逐月 write_incremental + seal_partition -> market/etf/raw/year=/month= 封存分区
  4. derive  aggregate_index_frame 重采样 weekly/monthly（带 is_partial，用交易日历判定）
             -> market/etf/csindex/<code>/<freq>/<freq>.parquet + 派生 manifest
  5. runlog  抓取/门禁/派生摘要写 state/data/runlog（在数据仓侧）

保留期：asset="etf" -> schema.RETENTION["etf"]=2430（10 年）。增量链末尾跑
expire_partitions(asset="etf") 顺手删旧（阶段 B 的 data-etf-incr.yml）。

用法：
    # 真实抓取（数据仓 workflow 内，data/ 已挂载）：
    python -m tools.data_pipeline.csindex --data-root data --writer "data-etf-firstload@run N"
    # 离线自测（合成 bars，不联网）：
    python -m tools.data_pipeline.csindex --offline --data-root /tmp/etf
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
import time

# 让 `python -m tools.data_pipeline.csindex` 与裸跑都能 import common
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# ★ ETF 资产类默认代码池（中证官网 index-perf 全历史）。行业指数可按需 --codes 覆盖。
CSINDEX_CODES = ("H20269", "H30269", "H00300", "000300")
GROUP = "csindex"                       # market/etf/<GROUP>/<code>/<freq>/
FQ = "raw"                              # 指数无前复权问题，daily 存 raw
DERIVED_FREQS = ("weekly", "monthly")
# 各指数最早可回溯日（官网接口不接受更早，否则返回空）。默认 2013-07-19（H20269 发布日）。
CSI_START = "20130719"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
API = "https://www.csindex.com.cn/csindex-home/perf/index-perf"


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


# ---------------------------------------------------------------------------
# 抓取（中证官网 index-perf，按年分段 + 退避重试）
# ---------------------------------------------------------------------------
def fetch_csi_rows(code: str, start: str, end: str, *, retries: int = 6,
                   logger=print) -> list:
    """中证官网 index-perf 单段抓取，返回 rows（tradeDate/open/high/low/close/...）。

    CI 境外 IP 偶发连接重置 -> 指数退避 + 抖动重试（domains/etf/update.py 同款）。
    """
    import urllib.request
    url = f"{API}?indexCode={code}&startDate={start}&endDate={end}"
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": "https://www.csindex.com.cn/"})
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode("utf-8"))
            rows = j.get("data") or []
            if rows or start != CSI_START:
                return rows
            last = ValueError("empty rows")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2.0 + 2.5 * k + 0.5 * ((k * 7919) % 10))
    raise RuntimeError(f"fetch CSI {code} failed: {last}")


def fetch_history(code: str, start: str, end: str, *, logger=print) -> list:
    """按年分段抓取全史（官网接口单次可跨多年，但分段降低超时风险），按 tradeDate 升序。"""
    s = _dt.date.fromisoformat(start)
    e = _dt.date.fromisoformat(end)
    out: list = []
    seen: set = set()
    cur = s
    while cur <= e:
        seg_end = min(cur.replace(year=cur.year + 1) - _dt.timedelta(days=1), e)
        if seg_end < cur:
            seg_end = e
        rows = fetch_csi_rows(code, cur.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d"), logger=logger)
        new = [r for r in rows if r.get("tradeDate") not in seen]
        for r in new:
            seen.add(r["tradeDate"])
        out += new
        logger(f"  [csindex] {code} {cur:%Y}-{seg_end:%Y}: +{len(new)} 根（累计 {len(out)}）")
        cur = seg_end + _dt.timedelta(days=1)
    out.sort(key=lambda r: r["tradeDate"])
    return out


# ---------------------------------------------------------------------------
# rows -> 契约 daily 帧
# ---------------------------------------------------------------------------
def bars_to_frame(rows_by_code: dict, pd=None):
    """{code: [rows,...]} -> 契约 daily 帧（asset="etf"）。

    ★ OHLC 口径（为什么 open/high/low 退化为 close）：
      官网对全收益指数（H 开头）不返回 open/high/low（null）。填 close 满足契约不变量
      low <= min(o,c) <= max(o,c) <= high，且不变量断言能过；但上层不得据此做日内因子。
    ★ amount：官网 tradingValue=亿元 -> ×1e8 = 元（契约）。
    ★ volume：官网 tradingVol=股（契约同单位，不换算）。
    """
    pd = pd or _pd()
    rows = []
    for code, rs in rows_by_code.items():
        for r in rs:
            try:
                close = float(r["close"])
            except (TypeError, ValueError):
                continue
            if close <= 0:
                continue
            o = _f(r.get("open"), close)
            h = _f(r.get("high"), close)
            l = _f(r.get("low"), close)
            # 官网偶发 OHLC 与 close 不一致（价格指数真实值），保证不变量恒真
            h = max(h, o, close)
            l = min(l, o, close)
            tv = _f(r.get("tradingVol"), 0.0)
            amt = _f(r.get("tradingValue"), float("nan"))
            if amt != amt:              # NaN -> 保持 NaN（不得填 0，上层会把 0 当零成交）
                amount = float("nan")
            else:
                amount = amt * 1e8
            rows.append({"code": code, "date": pd.Timestamp(r["tradeDate"]),
                         "open": o, "high": h, "low": l, "close": close,
                         "volume": int(tv), "amount": amount})
    cols = ["code", "date", "open", "high", "low", "close", "volume", "amount"]
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values(["code", "date"], kind="stable").reset_index(drop=True)


def _f(v, default: float) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        if f != f:
            return default
        return f
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 新鲜度门禁
# ---------------------------------------------------------------------------
def freshness_gate(daily_by_code: dict, calendar, asof, *, allow_stale: bool = False):
    """ETF 资产类 daily date_max 必须 = 最近已收盘交易日，否则红。"""
    cutoff = calendar.closed_only_cutoff(asof)
    problems = []
    detail = {}
    for code, df in daily_by_code.items():
        if df is None or len(df) == 0:
            problems.append(f"{code}: 空数据")
            detail[code] = None
            continue
        dmax = _dt.date.fromisoformat(str(pd_max_date(df))[:10])
        detail[code] = dmax.isoformat()
        if dmax < cutoff:
            problems.append(f"{code}: date_max {dmax} 落后最近交易日 {cutoff}")
    if not daily_by_code:
        problems.append("代码池为空")
    level = "red" if problems else "green"
    return level, {"cutoff": cutoff.isoformat(), "date_max": detail, "problems": problems}


def pd_max_date(df):
    return df["date"].max()


# ---------------------------------------------------------------------------
# 写入：daily 逐月封存
# ---------------------------------------------------------------------------
def backfill_daily(df, root: str, writer: str, pd=None) -> int:
    """全历史 daily -> 逐月 write_incremental + seal_partition（只用公共 API）。

    得到 market/etf/raw/year=YYYY/month=MM/batch=NN.parquet 封存分区。
    幂等：write_incremental 同月同分片行数一致即跳过；seal 重跑覆盖同分区（同数据同字节）。
    """
    pd = pd or _pd()
    from common.store.writer import write_incremental, seal_partition
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    months = sorted(set(zip(df["date"].dt.year.tolist(), df["date"].dt.month.tolist())))
    for (y, m) in months:
        g = df[(df["date"].dt.year == y) & (df["date"].dt.month == m)]
        if g.empty:
            continue
        first = g["date"].min().strftime("%Y%m%d")
        write_incremental(g, "etf", FQ, first, root=root, writer=writer,
                          allow_overwrite=True, pd=pd)
        seal_partition("etf", FQ, int(y), int(m), root=root, writer=writer, pd=pd)
    from tools.data_pipeline import prune_manifest
    prune_manifest(root, "etf", FQ)
    return len(months)


def ingest_daily_increment(df, day, root: str, writer: str, pd=None) -> str:
    """稳态：当日增量 write_incremental（append-only 日分片）。"""
    from common.store.writer import write_incremental
    return write_incremental(df, "etf", FQ, day, root=root, writer=writer, pd=pd or _pd())


# ---------------------------------------------------------------------------
# 派生：weekly / monthly
# ---------------------------------------------------------------------------
def daily_fingerprint(daily_df, pd=None) -> str:
    """daily 帧的确定性指纹（用于派生一致性断言 derived_from.sha256）。"""
    pd = pd or _pd()
    d = daily_df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    cols = [c for c in ("code", "date", "open", "high", "low", "close", "volume") if c in d.columns]
    d = d[cols].sort_values(["code", "date"], kind="stable")
    blob = d.to_csv(index=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def derive_period(code: str, freq: str, root: str, calendar, asof, writer: str, pd=None) -> dict:
    """daily -> weekly/monthly 物化到 market/etf/csindex/<code>/<freq>/，写派生 manifest。"""
    pd = pd or _pd()
    from common.store.reader import load
    from common.aggregate import aggregate_index_frame, AGGREGATOR_VERSION

    daily = load(asset="etf", fq=FQ, freq="daily", code=code, root=root)
    if daily is None or daily.empty:
        raise RuntimeError(f"no daily etf index for code={code} under {root}")

    agg = aggregate_index_frame(daily, code, freq, calendar=calendar, asof=asof, pd=pd)

    outdir = os.path.join(root, "market", "etf", GROUP, code, freq)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"{freq}.parquet")
    tmp = out + ".tmp"
    agg.to_parquet(tmp, index=False)
    os.replace(tmp, out)

    fp = daily_fingerprint(daily, pd)
    man = {
        "asset": "etf", "code": code, "freq": freq, "derived": True,
        "contract_version": "1.0",
        "rows": int(len(agg)),
        "partial_bars": int(agg["is_partial"].sum()) if "is_partial" in agg.columns else 0,
        "date_min": str(agg["date"].min().date()) if len(agg) else None,
        "date_max": str(agg["date"].max().date()) if len(agg) else None,
        "derived_from": {"freq": "daily", "fq": FQ, "rows": int(len(daily)), "sha256": fp},
        "aggregator": AGGREGATOR_VERSION,
        "rule": ("ISO-week|closed-only|calendar-aware" if freq == "weekly"
                 else "calendar-month|closed-only|calendar-aware"),
        "written_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "writer": writer,
        "path": os.path.relpath(out, root),
    }
    mdir = os.path.join(root, "manifest")
    os.makedirs(mdir, exist_ok=True)
    with open(os.path.join(mdir, f"etf_{GROUP}_{code}_{freq}.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    return man


# ---------------------------------------------------------------------------
# 编排主流程
# ---------------------------------------------------------------------------
def run(codes=CSINDEX_CODES, *, root="data", asof=None, writer="data-etf-firstload",
        offline=False, allow_stale=False, calendar=None, logger=print) -> dict:
    pd = _pd()
    asof = asof or _dt.date.today()
    if calendar is None:
        from common.calendar import load_calendar
        calendar = load_calendar(root=root)

    # ---- 1. 取 daily rows ----
    if offline:
        rows_by_code = _synthetic_rows(codes, pd)
    else:
        end = asof.isoformat()
        rows_by_code = {}
        for code in codes:
            logger(f"[fetch] {code} {CSI_START}..{end}")
            rows_by_code[code] = fetch_history(code, CSI_START, end, logger=logger)

    daily_df = bars_to_frame(rows_by_code, pd)
    if daily_df.empty:
        raise RuntimeError("抓取结果为空 —— 拒绝落盘（门禁）")
    daily_by_code = {c: daily_df[daily_df["code"] == c] for c in codes}

    # ---- 2. 新鲜度门禁 ----
    level, gate = freshness_gate(daily_by_code, calendar, asof, allow_stale=allow_stale)
    logger(f"[gate] freshness={level} cutoff={gate['cutoff']} date_max={gate['date_max']}")
    for p in gate["problems"]:
        logger(f"  [gate] {p}")
    if level == "red" and not allow_stale:
        raise RuntimeError(f"[新鲜度门禁] ETF 指数 daily 落后/缺失，中止且不落盘：{gate['problems']}")

    # ---- 3. 写 daily 封存分区 ----
    n_months = backfill_daily(daily_df, root, writer, pd)
    logger(f"[write] daily 封存 {n_months} 个月分区 -> market/etf/{FQ}/year=*/month=*")

    # ---- 4. 派生 weekly/monthly ----
    derived = []
    for code in codes:
        for freq in DERIVED_FREQS:
            man = derive_period(code, freq, root, calendar, asof, writer, pd)
            derived.append(man)
            logger(f"[derive] {code} {freq}: {man['rows']} 根（partial={man['partial_bars']}）"
                   f" -> {man['path']}")

    # ---- 5. runlog 摘要 ----
    summary = {
        "pipeline": "csindex", "asof": asof.isoformat(), "writer": writer,
        "asset": "etf", "codes": list(codes), "daily_rows": int(len(daily_df)),
        "months_sealed": n_months,
        "freshness": {"level": level, **gate},
        "derived": [{k: m[k] for k in ("code", "freq", "rows", "partial_bars", "path")}
                    for m in derived],
        "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
    }
    return summary


def _synthetic_rows(codes, pd):
    """离线自测：造 2026-07-01..09-11 的确定性合成 rows（不联网，OHLC 含真实值模拟）。"""
    out = {}
    days = [d for d in pd.date_range("2026-07-01", "2026-09-11", freq="B")]
    for i, code in enumerate(codes):
        base = 3000.0 + i * 1000
        rows = []
        px = base
        for j, d in enumerate(days):
            o = px
            c = px * (1 + 0.001 * ((j % 5) - 2))
            h = max(o, c) * 1.002
            l = min(o, c) * 0.998
            rows.append({"tradeDate": d.strftime("%Y%m%d"), "open": o, "high": h, "low": l,
                         "close": c, "tradingVol": 1_000_000 + j * 1000,
                         "tradingValue": (1_000_000 + j * 1000) * 25.0 / 1e8})
            px = c
        out[code] = rows
    return out


def write_runlog(summary: dict, root: str) -> str:
    d = os.path.join(root, "..", "state", "data", "runlog")
    d = os.path.normpath(d)
    if not os.path.isdir(os.path.dirname(d)):
        d = os.path.join(root, "state", "data", "runlog")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"etf_{summary['asof']}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="中证官网指数抓取编排（ETF 资产类）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--codes", default=",".join(CSINDEX_CODES))
    ap.add_argument("--asof", default=None, help="数据基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--writer", default="data-etf-firstload")
    ap.add_argument("--offline", action="store_true", help="合成 rows，不联网（自测）")
    ap.add_argument("--allow-stale", action="store_true", help="新鲜度红降级为告警（首灌/演练用）")
    ap.add_argument("--no-runlog", action="store_true")
    args = ap.parse_args(argv)

    codes = tuple(c.strip() for c in args.codes.split(",") if c.strip())
    asof = _dt.date.fromisoformat(args.asof) if args.asof else _dt.date.today()
    summary = run(codes, root=args.data_root, asof=asof, writer=args.writer,
                  offline=args.offline, allow_stale=args.allow_stale)
    if not args.no_runlog:
        try:
            p = write_runlog(summary, args.data_root)
            print(f"[runlog] {p}")
        except Exception as e:  # noqa: BLE001
            print(f"[runlog] 写入失败（不阻断）：{e}")
    print(json.dumps({k: summary[k] for k in ("daily_rows", "months_sealed", "freshness")},
                     ensure_ascii=False, indent=2))
    print(f"[✓] csindex pipeline done: {summary['daily_rows']} daily rows, "
          f"{len(summary['derived'])} derived partitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
