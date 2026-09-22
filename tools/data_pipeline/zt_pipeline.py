#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""涨停复盘事件型数据：自算（全历史）+ 东财快照增强（近10交易日）。

三张表（事件型存储，读写走 common/store/events.py）：
  - zt_daily  涨停自算：raw 日K 按板块阈值判定涨停，派生连板数/近3/5/10日涨停次数，
              全历史回补 + 日增量（增量只算最新交易日，窗口读历史尾部）
  - zt_ladder 连板梯队/晋级率：昨日 (k-1) 板 -> 今日 k 板 的晋升比例，自算派生
  - zt_pool   东财涨停池快照（push2ex getTopicZTPool）：封单资金/首末封板时间/炸板次数/
              行业板块/N天M板——★源仅保留近 ~10 个交易日，历史无法回补（已实测）

已知口径限制（方案决策 2026-09-22）：
  1. ST 股 5% 涨停不特殊识别（universe 无 ST 标记），按板块阈值判；漏判的 ST 5% 板
     已在 README 数据说明中标注
  2. 涨停原因/题材字段：东财/同花顺均无历史公开接口，未入库（用户已确认接受"主干方案"）
  3. 封单资金/封板时间只有近10日快照有；更早历史查 zt_daily 自算列

用法：
    python -m tools.data_pipeline.zt_pipeline --data-root data --writer "data-events@run N"   # 日增量
    python -m tools.data_pipeline.zt_pipeline --data-root data --backfill 2023-09-01          # 历史回补
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np

POOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
POOL_UT = "7eea3edcaed734bea9cbfc24409ed989"
POOL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
# 自算前导窗口：m10 最大窗口 10 + 连板追溯，留 5 个交易日余量
LEAD_TRADE_DAYS = 15
ZT_COLS = ["date", "code", "name", "close", "change_rate", "limit_up", "limit_count",
           "m3", "m5", "m10", "threshold"]


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


# ---------------------------------------------------------------------------
# 涨停判定
# ---------------------------------------------------------------------------
def limit_threshold(code: str) -> float:
    """板块涨停阈值（%）：创业板/科创板 20、北交所 30、主板 10。"""
    if code.startswith(("688", "689", "300", "301", "302")):
        return 20.0
    if code.startswith(("4", "8")):
        return 30.0
    return 10.0


def _round2(x):
    """四舍五入到分（涨停价 = round(prev_close * (1+thr), 2)）。"""
    return (x + 1e-9).round(2)


def _streak_lc(lu) -> list:
    """连板数游程递推：limit_count[i] = limit_count[i-1]+1 if 涨停 else 0。

    注意必须用前一日 limit_count（而非前一日 limit_up）累加，否则 3 板以上
    会被错误压成 2 板（2026-09-21 华瓷股份 5 连板实测踩坑）。
    """
    lc = 0
    out = []
    for v in lu:
        if v:
            lc += 1
        else:
            lc = 0
        out.append(lc)
    return out


# ---------------------------------------------------------------------------
# 自算（全量：回补用）
# ---------------------------------------------------------------------------
def compute_zt_full(raw, uni_name: dict, pd):
    """raw 全量日K -> 全量 zt_daily 帧（含 limit_up/limit_count/m3/m5/m10）。"""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    df["prev_close"] = df.groupby("code")["close"].shift(1)
    df["threshold"] = df["code"].map(limit_threshold)
    df["change_rate"] = (df["close"] / df["prev_close"] - 1.0) * 100.0
    df["limit_up"] = (
        (df["close"] >= _round2(df["prev_close"] * (1.0 + df["threshold"] / 100.0)) - 0.001)
        & df["prev_close"].notna()
    ).astype("int64")
    df["limit_count"] = (df.groupby("code", sort=False)["limit_up"]
                           .transform(_streak_lc).astype("int64"))
    df["m3"] = df.groupby("code")["limit_up"].transform(
        lambda s: s.rolling(3, min_periods=1).sum().astype("int64"))
    df["m5"] = df.groupby("code")["limit_up"].transform(
        lambda s: s.rolling(5, min_periods=1).sum().astype("int64"))
    df["m10"] = df.groupby("code")["limit_up"].transform(
        lambda s: s.rolling(10, min_periods=1).sum().astype("int64"))
    df["name"] = df["code"].map(uni_name)
    return df[ZT_COLS]


# ---------------------------------------------------------------------------
# 自算（单日：增量用，窗口 = 前导 raw + 已入库 zt 尾部）
# ---------------------------------------------------------------------------
def compute_day(target: _dt.date, raw_win, hist_zt, uni_name: dict, pd):
    """算 target 日 zt_daily。

    raw_win : 含 target 及其前 ~15 个交易日的全A raw（date/code/close）
    hist_zt : 已入库 zt_daily（date/code/limit_up/limit_count），target 之前的最近窗口
    返回仅含 target 日的 zt_daily 帧。
    """
    raw = raw_win.copy()
    raw = raw.sort_values(["code", "date"]).reset_index(drop=True)
    raw["prev_close"] = raw.groupby("code")["close"].shift(1)
    raw["threshold"] = raw["code"].map(limit_threshold)
    raw["change_rate"] = (raw["close"] / raw["prev_close"] - 1.0) * 100.0
    raw["limit_up"] = (
        (raw["close"] >= _round2(raw["prev_close"] * (1.0 + raw["threshold"] / 100.0)) - 0.001)
        & raw["prev_close"].notna()
    ).astype("int64")
    t = raw[raw["date"] == pd.Timestamp(target)].copy()
    t["limit_count"] = 0  # 占位，tail() 会按连板追溯重算

    if hist_zt is None or hist_zt.empty:
        hist = pd.DataFrame(columns=["code", "date", "limit_up", "limit_count"])
    else:
        hist = hist_zt[["code", "date", "limit_up", "limit_count"]]
    seq = pd.concat([hist, t[["code", "date", "limit_up", "limit_count", "change_rate",
                              "threshold", "close"]]], ignore_index=True)
    seq = seq.sort_values(["code", "date"]).reset_index(drop=True)

    def tail(g):
        g = g.tail(LEAD_TRADE_DAYS)
        lu = g["limit_up"].tolist()
        m3, m5, m10 = sum(lu[-3:]), sum(lu[-5:]), sum(lu[-10:])
        lc = 0
        for v in reversed(lu):
            if v:
                lc += 1
            else:
                break
        g = g.iloc[[-1]].copy()
        g["limit_count"] = lc
        g["m3"], g["m5"], g["m10"] = m3, m5, m10
        return g

    out = seq.groupby("code", group_keys=False).apply(tail)
    out["name"] = out["code"].map(uni_name)
    return out[ZT_COLS].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 连板梯队 / 晋级率
# ---------------------------------------------------------------------------
def build_ladder(today_zt, prev_zt, date: _dt.date, pd):
    """今日 k 板分布 vs 昨日 (k-1) 板分布 -> 晋级率行。"""
    dist_t = today_zt[today_zt["limit_count"] > 0]["limit_count"].value_counts()
    dist_p = prev_zt[prev_zt["limit_count"] > 0]["limit_count"].value_counts()
    rows = []
    for k, cnt in dist_t.items():
        pb = int(dist_p.get(k - 1, 0))
        rows.append({
            "date": date,
            "lbc": int(k),
            "count": int(cnt),
            "prev_count": pb,
            "promote_rate": (float(cnt) / pb if pb else None),
        })
    return pd.DataFrame(rows)


def build_ladder_full(zt, pd):
    """全量 zt_daily -> 全量 zt_ladder（相邻交易日递推）。"""
    z = zt[zt["limit_count"] > 0].sort_values("date")
    if z.empty:
        return pd.DataFrame()
    dates = sorted(z["date"].unique())
    groups = {d: g for d, g in z.groupby("date")}
    rows = []
    for i, t in enumerate(dates):
        if i == 0:
            continue
        pt = dates[i - 1]
        dist_t = groups[t]["limit_count"].value_counts()
        dist_p = groups[pt]["limit_count"].value_counts()
        for k, cnt in dist_t.items():
            pb = int(dist_p.get(k - 1, 0))
            rows.append({
                "date": t,
                "lbc": int(k),
                "count": int(cnt),
                "prev_count": pb,
                "promote_rate": (float(cnt) / pb if pb else None),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 东财涨停池快照（近 ~10 交易日）
# ---------------------------------------------------------------------------
def fetch_pool(day: _dt.date, *, session=None, logger=print):
    """抓单个交易日涨停池快照 -> zt_pool 帧（无数据返回空帧）。"""
    import requests
    pd = _pd()
    params = {
        "ut": POOL_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 2000,
        "sort": "fbt:asc", "date": day.strftime("%Y%m%d"),
        "_": int(time.time() * 1000),
    }
    s = session or requests
    r = s.get(POOL_URL, params=params, headers=POOL_HEADERS, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("rc") != 0:
        raise RuntimeError(f"push2ex 报错 rc={j.get('rc')}")
    pool = (j.get("data") or {}).get("pool") or []
    rows = []
    for p in pool:
        zttj = p.get("zttj") or {}
        rows.append({
            "date": day,
            "code": str(p.get("c") or ""),
            "name": p.get("n"),
            "price": p.get("p") / 1000.0 if p.get("p") else None,   # 分 -> 元
            "change_rate": p.get("zdp"),
            "amount": p.get("amount"),
            "free_market_cap": p.get("ltsz"),
            "total_market_cap": p.get("tshare"),
            "turnover_rate": p.get("hs"),
            "seal_amount": p.get("fund"),
            "first_seal_time": p.get("fbt"),
            "last_seal_time": p.get("lbt"),
            "break_count": p.get("zbc"),
            "limit_board_count": p.get("lbc"),
            "zt_days": zttj.get("days"),
            "zt_count": zttj.get("ct"),
            "industry": p.get("hybk"),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _day_has(root: str, table: str, day) -> bool:
    from common.store.events import load_events
    df = load_events(table, dates=[day], columns=[], root=root, strict=False)
    return not df.empty


def _uni_name(root: str, pd):
    from common.store.reader import load_meta, normalize_code
    u = load_meta("universe", root=root, pd=pd)
    return {normalize_code(c): n for c, n in zip(u["code"], u["name"])}


def _load_raw_window(root: str, end, n_days, calendar, pd):
    """读最近 n_days 个交易日的全A raw（含 end），列 date/code/close。"""
    from common.store.reader import load
    start = calendar.shift(end, -(n_days - 1))
    df = load(asset="stock", fq="raw", start=start.isoformat(), end=end.isoformat(),
              columns=["close"], root=root, strict=False)
    return df


# ---------------------------------------------------------------------------
# 增量
# ---------------------------------------------------------------------------
def run_incr(*, root="data", asof=None, writer="data-events", calendar=None, logger=print) -> dict:
    from common.calendar import load_calendar
    from common.store.events import load_events, write_event
    pd = _pd()
    calendar = calendar or load_calendar(root=root)
    asof = asof or _dt.date.today()
    target = calendar.closed_only_cutoff(asof)
    prev_day = calendar.shift(target, -1)  # 前一交易日（last_trading_day(target)==target 本身）
    logger(f"[gate] asof={asof} target_day={target} prev={prev_day}")

    uni = _uni_name(root, pd)
    done = {"pool": False, "daily": False, "ladder": False}

    # 1) 涨停池快照（近10日窗口自动滚动）
    if _day_has(root, "zt_pool", target):
        logger(f"[skip] {target} zt_pool 已入库")
        done["pool"] = True
    else:
        try:
            pool = fetch_pool(target, logger=logger)
            if pool.empty:
                logger(f"[warn] {target} zt_pool 为空（可能超出近10日窗口/非交易日）")
            else:
                p = write_event(pool, "zt_pool", target, root=root, writer=writer, pd=pd)
                logger(f"[write] zt_pool {len(pool)} 行 -> {p}")
                done["pool"] = True
        except Exception as e:  # noqa: BLE001
            logger(f"[warn] zt_pool 抓取失败（不阻断自算）: {e}")

    # 2) 自算当日 zt_daily
    daily = None
    if _day_has(root, "zt_daily", target):
        logger(f"[skip] {target} zt_daily 已入库")
        done["daily"] = True
    else:
        raw_win = _load_raw_window(root, target, LEAD_TRADE_DAYS + 1, calendar, pd)
        hist = load_events("zt_daily", start=(target - _dt.timedelta(days=40)).isoformat(),
                           columns=["code", "date", "limit_up", "limit_count"],
                           root=root, strict=False, pd=pd)
        if not hist.empty:
            last_dates = sorted(hist["date"].unique())[-LEAD_TRADE_DAYS:]
            hist = hist[hist["date"].isin(last_dates)]
        daily = compute_day(target, raw_win, hist, uni, pd)
        if daily.empty:
            raise RuntimeError(f"{target} zt_daily 自算为空（raw 无数据），拒绝落盘")
        p = write_event(daily, "zt_daily", target, root=root, writer=writer, pd=pd)
        logger(f"[write] zt_daily {len(daily)} 行 -> {p}")
        done["daily"] = True

    # 3) 当日连板梯队/晋级率（独立于 step2：daily 已存在但 ladder 缺失时也要补齐）
    if _day_has(root, "zt_ladder", target):
        logger(f"[skip] {target} zt_ladder 已入库")
        done["ladder"] = True
    else:
        if daily is None:
            daily = load_events("zt_daily", dates=[target], root=root, strict=False, pd=pd)
        prev_zt = load_events("zt_daily", dates=[prev_day],
                              columns=["code", "limit_count"], root=root,
                              strict=False, pd=pd)
        ladder = build_ladder(daily, prev_zt, target, pd)
        if not ladder.empty:
            p = write_event(ladder, "zt_ladder", target, root=root, writer=writer, pd=pd)
            logger(f"[write] zt_ladder {len(ladder)} 行 -> {p}")
            done["ladder"] = True

    return {"pipeline": "zt_incr", "asof": asof.isoformat(), "target_day": target.isoformat(),
            "writer": writer, "done": done,
            "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}


# ---------------------------------------------------------------------------
# 历史回补（自算全量按月封存；快照仅近10日）
# ---------------------------------------------------------------------------
def run_backfill(*, root="data", start: str, asof=None, writer="zt-backfill",
                 calendar=None, logger=print) -> dict:
    from common.calendar import load_calendar
    from common.store.events import write_event, write_event_month
    from common.store.reader import load
    pd = _pd()
    calendar = calendar or load_calendar(root=root)
    asof = asof or _dt.date.today()
    target = calendar.closed_only_cutoff(asof)
    s0 = _dt.date.fromisoformat(start)

    uni = _uni_name(root, pd)

    # 1) 自算 zt_daily（全量 raw -> 按月封存）
    lead_start = calendar.shift(s0, -LEAD_TRADE_DAYS)
    raw = load(asset="stock", fq="raw", start=lead_start.isoformat(), end=target.isoformat(),
               columns=["close"], root=root, strict=False)
    logger(f"[raw] {lead_start} .. {target} 全A raw {len(raw):,} 行")
    zt = compute_zt_full(raw, uni, pd)
    zt = zt[pd.to_datetime(zt["date"]).dt.date >= s0]
    logger(f"[zt_daily] 自算 {len(zt):,} 行（{len(zt['date'].unique())} 个交易日）")
    by_month = {}
    for d, g in zt.groupby([pd.to_datetime(zt["date"]).dt.year,
                            pd.to_datetime(zt["date"]).dt.month]):
        by_month[d] = g
    for (y, m), g in sorted(by_month.items()):
        r = write_event_month(g, "zt_daily", y, m, root=root, writer=writer, pd=pd)
        logger(f"[seal] zt_daily {y}-{m:02d} {r['rows']:,} 行")

    # 2) 连板梯队/晋级率（全量递推 -> 按月封存）
    ladder = build_ladder_full(zt, pd)
    logger(f"[zt_ladder] 晋级率 {len(ladder):,} 行")
    for (y, m), g in ladder.groupby([pd.to_datetime(ladder["date"]).dt.year,
                                     pd.to_datetime(ladder["date"]).dt.month]):
        r = write_event_month(g, "zt_ladder", y, m, root=root, writer=writer, pd=pd)
        logger(f"[seal] zt_ladder {y}-{m:02d} {r['rows']:,} 行")

    # 3) 涨停池快照：仅近 ~10 个交易日可回补
    pool_days = calendar.range(calendar.shift(target, -9), target)
    pool_fetched = 0
    import requests
    session = requests.Session()
    session.trust_env = False
    session.headers.update(POOL_HEADERS)
    for day in pool_days:
        if _day_has(root, "zt_pool", day):
            continue
        try:
            pool = fetch_pool(day, session=session, logger=logger)
            if pool.empty:
                continue
            write_event(pool, "zt_pool", day, root=root, writer=writer, pd=pd)
            pool_fetched += 1
            time.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            logger(f"  [warn] {day} zt_pool 失败: {e}")
    logger(f"[zt_pool] 近10日快照补抓 {pool_fetched} 日")

    return {"pipeline": "zt_backfill", "start": s0.isoformat(), "end": target.isoformat(),
            "zt_daily_rows": int(len(zt)), "zt_ladder_rows": int(len(ladder)),
            "pool_days": pool_fetched, "writer": writer,
            "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}


# ---------------------------------------------------------------------------
def write_runlog(summary: dict, root: str) -> str:
    d = os.path.join(root, "..", "state", "data", "runlog")
    d = os.path.normpath(d)
    if not os.path.isdir(os.path.dirname(d)):
        d = os.path.join(root, "state", "data", "runlog")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"zt_{summary.get('target_day') or summary.get('end')}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="涨停复盘事件型数据（自算+快照）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--asof", default=None, help="数据基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--backfill", default=None, metavar="YYYY-MM-DD",
                    help="历史回补起始日（自算全量按月封存；快照仅近10日）")
    ap.add_argument("--writer", default="data-events")
    ap.add_argument("--no-runlog", action="store_true")
    args = ap.parse_args(argv)

    asof = _dt.date.fromisoformat(args.asof) if args.asof else _dt.date.today()
    if args.backfill:
        summary = run_backfill(root=args.data_root, start=args.backfill, asof=asof,
                               writer=args.writer)
    else:
        summary = run_incr(root=args.data_root, asof=asof, writer=args.writer)
    if not args.no_runlog:
        try:
            p = write_runlog(summary, args.data_root)
            print(f"[runlog] {p}")
        except Exception as e:  # noqa: BLE001
            print(f"[runlog] 写入失败（不阻断）：{e}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("[✓] zt done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
