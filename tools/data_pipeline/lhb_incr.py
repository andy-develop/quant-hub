#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""龙虎榜事件型数据：日级增量 + 历史回补（events/lhb_detail + events/lhb_seat）。

数据源：东财 datacenter（公开，无需登录，实测支持任意历史交易日）：
  - 主表   RPT_DAILYBILLBOARD_DETAILSNEW   净买额/上榜原因/买卖总额/机构说明
  - 买入席位 RPT_BILLBOARD_DAILYDETAILSBUY   营业部名/买卖额
  - 卖出席位 RPT_BILLBOARD_DAILYDETAILSSELL  营业部名/买卖额

存储：data/events/lhb_detail/、data/events/lhb_seat/（事件型，主键含 TRADE_ID，
一票多因不冲突；读写走 common/store/events.py，manifest 在 manifest/events_*.json）。

用法：
    # 日增量（workflow 内，抓最近已收盘交易日）：
    python -m tools.data_pipeline.lhb_incr --data-root data --writer "data-events@run N"
    # 历史回补（近3年：2023-09-01 起逐交易日抓取，按月封存）：
    python -m tools.data_pipeline.lhb_incr --data-root data --backfill 2023-09-01
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REPORT_DETAIL = "RPT_DAILYBILLBOARD_DETAILSNEW"
REPORT_SEATS = (("buy", "RPT_BILLBOARD_DAILYDETAILSBUY"),
                ("sell", "RPT_BILLBOARD_DAILYDETAILSSELL"))
BASE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
}
PAGE_SIZE = 500
SLEEP = 0.15          # 请求间隔（秒），datacenter 无硬限流，留余量
FETCH_RETRY = 3


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


def _fetch(report: str, day: str, page: int, *, session=None, logger=print) -> dict:
    params = {
        "reportName": report, "columns": "ALL",
        "filter": f"(TRADE_DATE='{day}')",
        "pageNumber": page, "pageSize": PAGE_SIZE,
        "sortColumns": "TRADE_DATE", "sortTypes": "-1",
        "source": "WEB", "client": "WEB",
    }
    import requests
    s = session or requests
    last = None
    for attempt in range(1, FETCH_RETRY + 1):
        try:
            r = s.get(BASE_URL, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            j = r.json()
            if j.get("success") is False:
                msg = str(j.get("message") or "")
                # 数据侧合法空（停市/当日盘后未公布）≠ 抓取失败：按空页处理
                if "数据为空" in msg or "无数据" in msg:
                    return {}
                raise RuntimeError(f"datacenter 报错: {msg}")
            return j.get("result") or {}
        except Exception as e:  # noqa: BLE001
            last = e
            logger(f"  [fetch] {report} {day} p{page} 第 {attempt}/{FETCH_RETRY} 次失败: {e}")
            time.sleep(1.0 * attempt)
    raise RuntimeError(f"抓取失败 {report} {day} p{page}: {last}")


# ---------------------------------------------------------------------------
# 单日抓取 -> 两个契约帧
# ---------------------------------------------------------------------------
def _num(v):
    return None if v is None else float(v)


def _detail_frame(rows, pd):
    if not rows:
        return pd.DataFrame()
    out = []
    for r in rows:
        out.append({
            "date": str(r.get("TRADE_DATE"))[:10],
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": r.get("SECURITY_NAME_ABBR"),
            "market": r.get("MARKET"),
            "trade_id": r.get("TRADE_ID"),
            "reason": r.get("EXPLANATION"),
            "reason_tag": r.get("EXPLAIN"),
            "change_type": r.get("CHANGE_TYPE"),
            "close": _num(r.get("CLOSE_PRICE")),
            "change_rate": _num(r.get("CHANGE_RATE")),
            "turnover_rate": _num(r.get("TURNOVERRATE")),
            "free_market_cap": _num(r.get("FREE_MARKET_CAP")),
            "acc_amount": _num(r.get("ACCUM_AMOUNT")),
            "buy_amount": _num(r.get("BILLBOARD_BUY_AMT")),
            "sell_amount": _num(r.get("BILLBOARD_SELL_AMT")),
            "net_amount": _num(r.get("BILLBOARD_NET_AMT")),
        })
    return pd.DataFrame(out)


def _seat_frame(rows, pd):
    if not rows:
        return pd.DataFrame()
    out = []
    for r in rows:
        out.append({
            "date": str(r.get("TRADE_DATE"))[:10],
            "code": str(r.get("SECURITY_CODE") or ""),
            "trade_id": r.get("TRADE_ID"),
            "side": r.get("_side"),
            "seat_code": str(r.get("OPERATEDEPT_CODE") or ""),
            "seat_name": r.get("OPERATEDEPT_NAME"),
            "buy": _num(r.get("BUY")),
            "sell": _num(r.get("SELL")),
            "net": _num(r.get("NET")),
            "rank": 0,
        })
    df = pd.DataFrame(out)
    if not df.empty:
        # rank：buy 方向按买入额降序、sell 方向按卖出额降序，组内从 1 起
        key = df["buy"].fillna(0).where(df["side"] == "buy", df["sell"].fillna(0))
        df = df.assign(_key=key)
        df = df.sort_values(["date", "code", "trade_id", "side", "_key"],
                            ascending=[True, True, True, True, False], kind="stable")
        df["rank"] = df.groupby(["date", "code", "trade_id", "side"], sort=False).cumcount() + 1
        df = df.drop(columns=["_key"])
    return df


def fetch_day(day: _dt.date, *, session=None, logger=print):
    """抓单个交易日 -> (detail_df, seat_df)。"""
    pd = _pd()
    ds = day.isoformat()
    details: list = []
    pn = 1
    while True:
        res = _fetch(REPORT_DETAIL, ds, pn, session=session, logger=logger)
        rows = res.get("data") or []
        if not rows:
            break
        details += rows
        if pn >= int(res.get("pages") or 1):
            break
        pn += 1
        time.sleep(SLEEP)
    seats: list = []
    for side, rep in REPORT_SEATS:
        pn = 1
        while True:
            res = _fetch(rep, ds, pn, session=session, logger=logger)
            rows = res.get("data") or []
            if not rows:
                break
            for r in rows:
                r["_side"] = side
            seats += rows
            if pn >= int(res.get("pages") or 1):
                break
            pn += 1
            time.sleep(SLEEP)
        time.sleep(SLEEP)
    return _detail_frame(details, pd), _seat_frame(seats, pd)


# ---------------------------------------------------------------------------
# 写入（幂等：该日已有数据则跳过）
# ---------------------------------------------------------------------------
def _day_has(root: str, table: str, day) -> bool:
    from common.store.events import load_events
    df = load_events(table, dates=[day], columns=[], root=root, strict=False)
    return not df.empty


# ---------------------------------------------------------------------------
# 日增量
# ---------------------------------------------------------------------------
def run_incr(*, root="data", asof=None, writer="data-events", calendar=None,
             logger=print) -> dict:
    from common.calendar import load_calendar
    pd = _pd()
    calendar = calendar or load_calendar(root=root)
    asof = asof or _dt.date.today()
    target = calendar.closed_only_cutoff(asof)
    logger(f"[gate] asof={asof} target_day={target}（最近已收盘交易日）")

    if _day_has(root, "lhb_detail", target):
        logger(f"[skip] {target} 龙虎榜已入库，跳过")
        return {"pipeline": "lhb_incr", "asof": asof.isoformat(), "target_day": target.isoformat(),
                "skipped": True, "writer": writer,
                "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}

    d, s = fetch_day(target, logger=logger)
    if d.empty:
        raise RuntimeError(f"{target} 龙虎榜主表为空（非交易日或源异常），拒绝落盘")
    from common.store.events import write_event
    p1 = write_event(d, "lhb_detail", target, root=root, writer=writer, pd=pd)
    p2 = write_event(s, "lhb_seat", target, root=root, writer=writer, pd=pd)
    logger(f"[write] detail {len(d)} 行 -> {p1}")
    logger(f"[write] seat {len(s)} 行 -> {p2}")
    return {"pipeline": "lhb_incr", "asof": asof.isoformat(), "target_day": target.isoformat(),
            "detail_rows": int(len(d)), "seat_rows": int(len(s)), "writer": writer,
            "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}


# ---------------------------------------------------------------------------
# 历史回补（按月封存，整月重建幂等）
# ---------------------------------------------------------------------------
def run_backfill(*, root="data", start: str, asof=None, writer="lhb-backfill",
                 calendar=None, logger=print) -> dict:
    from common.calendar import load_calendar
    from common.store.events import write_event_month
    pd = _pd()
    calendar = calendar or load_calendar(root=root)
    asof = asof or _dt.date.today()
    target = calendar.closed_only_cutoff(asof)
    s0 = _dt.date.fromisoformat(start)
    days = calendar.range(s0, target)
    logger(f"[backfill] {s0} .. {target} 共 {len(days)} 个交易日")

    skipped = fetched = 0
    # month -> (detail_df, seat_df) 累积；跨月时 flush 上月
    month_buf: dict[tuple, list] = {}
    cur_month: tuple | None = None

    def flush_all(m: tuple):
        items = month_buf.pop(m)
        dlist = [x[0] for x in items]
        slist = [x[1] for x in items]
        det = pd.concat(dlist, ignore_index=True) if dlist else pd.DataFrame()
        seats = pd.concat(slist, ignore_index=True) if slist else pd.DataFrame()
        r1 = write_event_month(det, "lhb_detail", m[0], m[1], root=root, writer=writer, pd=pd)
        r2 = write_event_month(seats, "lhb_seat", m[0], m[1], root=root, writer=writer, pd=pd)
        logger(f"[seal] {m[0]}-{m[1]:02d} detail {r1['rows']:,} 行 · seat {r2['rows']:,} 行")

    import requests
    session = requests.Session()
    session.trust_env = False
    session.headers.update(HEADERS)

    for day in days:
        m = (day.year, day.month)
        if cur_month is not None and m != cur_month:
            flush_all(cur_month)
        cur_month = m
        if _day_has(root, "lhb_detail", day):
            skipped += 1
            continue
        d, s = fetch_day(day, session=session, logger=logger)
        if d.empty:
            logger(f"  [warn] {day} 无龙虎榜数据（可能停市），跳过")
            continue
        month_buf.setdefault(m, []).append((d, s))
        fetched += 1
        if fetched % 20 == 0:
            logger(f"  [..] 已抓 {fetched} 日（跳过 {skipped}）")
        time.sleep(SLEEP)
    if cur_month is not None and cur_month in month_buf:
        flush_all(cur_month)

    logger(f"[done] 抓取 {fetched} 日 · 幂等跳过 {skipped} 日")
    return {"pipeline": "lhb_backfill", "start": s0.isoformat(), "end": target.isoformat(),
            "fetched_days": fetched, "skipped_days": skipped, "writer": writer,
            "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}


# ---------------------------------------------------------------------------
def write_runlog(summary: dict, root: str) -> str:
    d = os.path.join(root, "..", "state", "data", "runlog")
    d = os.path.normpath(d)
    if not os.path.isdir(os.path.dirname(d)):
        d = os.path.join(root, "state", "data", "runlog")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"lhb_{summary.get('target_day') or summary.get('end')}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="龙虎榜事件型数据（日增量/历史回补）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--asof", default=None, help="数据基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--backfill", default=None, metavar="YYYY-MM-DD",
                    help="历史回补起始日（逐交易日抓到最新已收盘日，按月封存）")
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
    print("[✓] lhb done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
