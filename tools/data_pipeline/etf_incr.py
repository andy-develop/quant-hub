#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 资产类日级增量（方案 §2.4 data-etf.yml 的 16:55 步，阶段 B）。

与 csindex.py（首载：全史抓取 + 全量封存）互补，本脚本只做增量窗口：
库内 date_max+1 .. target_day（最近已收盘交易日）。复用 csindex 的
fetch/bars_to_frame/derive_period/ingest_daily_increment，避免口径漂移。

流程：
  1. gate     target_day = 最近已收盘交易日（calendar.closed_only_cutoff）
  2. prev     库内各 code daily date_max（reader.load；封存月 + _incr 自动聚合）
  3. fetch    增量窗口 (date_max, target_day] 按年分段抓中证官网（幂等：已到 target_day 的 code 跳过）
  4. filter   交易日过滤（★ 幻影行根治，与 csindex 首载同款）
  5. gate     date_max 必须 = target_day（红 -> 中止不落盘，allow_stale 降级）
  6. write    write_incremental(etf,raw,当日) 幂等落 _incr/YYYYMMDD/raw.parquet
  7. derive   重物化 weekly/monthly（覆盖 + manifest，derived_from.sha256 断言）
  8. expire   顺手 expire_partitions(asset="etf", 2430 交易日) 删旧（Q5）
  9. runlog   state/data/runlog/etf_incr_<asof>.json

用法：
    python -m tools.data_pipeline.etf_incr --data-root data --writer "data-etf-incr@run N"
    python -m tools.data_pipeline.etf_incr --offline --data-root /tmp/etf
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.data_pipeline.csindex import (  # noqa: E402
    CSINDEX_CODES, CSI_START, DERIVED_FREQS, FQ, bars_to_frame,
    derive_period, fetch_csi_rows, freshness_gate, ingest_daily_increment,
)

ASSET = "etf"
RETENTION_TRADE_DAYS = 2430   # 方案 Q2：普通指数 10 年；基准指数（asset="index"）另由 index 链路全史保留


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


def _last_by_code(root: str, pd) -> dict:
    """库内各 code 的 daily date_max（空库返回 {}）。"""
    from common.store.reader import load
    daily = load(asset=ASSET, fq=FQ, freq="daily", root=root)
    if daily is None or len(daily) == 0:
        return {}
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    return {str(c): dd.date() for c, dd in d.groupby("code")["date"].max().items()}


def _fetch_incremental(code: str, start: _dt.date, end: _dt.date, *, logger=print) -> list:
    """增量窗口抓取（按年分段；start==库内 date_max+1）。"""
    out: list = []
    s = start
    while s <= end:
        seg_end = min(s.replace(year=s.year + 1) - _dt.timedelta(days=1), end)
        if seg_end < s:
            seg_end = end
        rows = fetch_csi_rows(code, s.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d"), logger=logger)
        out += rows
        logger(f"  [etf_incr] {code} {s:%Y}-{seg_end:%Y}: +{len(rows)} 根（累计 {len(out)}）")
        s = seg_end + _dt.timedelta(days=1)
    # tradeDate 去重升序（增量窗口内理论上无重复，防御性去重）
    seen: set = set()
    dedup = []
    for r in sorted(out, key=lambda r: r["tradeDate"]):
        if r.get("tradeDate") in seen:
            continue
        seen.add(r["tradeDate"])
        dedup.append(r)
    return dedup


def run(codes=CSINDEX_CODES, *, root="data", asof=None, writer="data-etf-incr",
        offline=False, allow_stale=False, expire=True, calendar=None, logger=print) -> dict:
    pd = _pd()
    asof = asof or _dt.date.today()
    if calendar is None:
        from common.calendar import load_calendar
        calendar = load_calendar(root=root)
    target_day = calendar.closed_only_cutoff(asof)
    logger(f"[gate] asof={asof} target_day={target_day}（最近已收盘交易日）")

    # ---- 幂等：目标日分片已存在 -> 跳过 ----
    inc = os.path.join(root, "market", ASSET, FQ, "_incr",
                       target_day.strftime("%Y%m%d"), "raw.parquet")
    if os.path.exists(inc):
        logger(f"[skip] {target_day} etf 增量已入库（{inc}），跳过抓取与写入")
        summary = {"pipeline": "etf_incr", "asof": asof.isoformat(), "writer": writer,
                   "asset": ASSET, "target_day": target_day.isoformat(),
                   "skipped": True, "reason": "increment exists",
                   "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}
        if expire:
            _expire(root, target_day, writer, logger, summary)
        return summary

    # ---- 1. 库内 date_max ----
    last_by_code = {} if offline else _last_by_code(root, pd)
    logger(f"[prev] 库内 date_max: { {c: str(d) for c, d in last_by_code.items()} or '空库' }")

    # ---- 2. 增量窗口抓取 ----
    end = target_day
    rows_by_code: dict = {}
    for code in codes:
        start = last_by_code.get(code)
        start = (start + _dt.timedelta(days=1)) if start else _dt.date.fromisoformat(CSI_START)
        if start > end:
            logger(f"[skip] {code} 已到 target_day（库内 {last_by_code[code]}），无增量")
            continue
        logger(f"[fetch] {code} {start}..{end}")
        if offline:
            rows_by_code[code] = _synthetic_rows(code, start, end)
        else:
            rows_by_code[code] = _fetch_incremental(code, start, end, logger=logger)

    if not rows_by_code:
        logger("[skip] 所有 code 均无增量窗口")
        summary = {"pipeline": "etf_incr", "asof": asof.isoformat(), "writer": writer,
                   "asset": ASSET, "target_day": target_day.isoformat(),
                   "skipped": True, "reason": "no increment window",
                   "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}
        if expire:
            _expire(root, target_day, writer, logger, summary)
        return summary

    daily_df = bars_to_frame(rows_by_code, pd)
    if daily_df.empty:
        raise RuntimeError("增量抓取结果为空 —— 拒绝落盘（门禁）")

    # ---- 3. 交易日过滤（★ 幻影行根治，与 csindex 首载同款） ----
    n_before = len(daily_df)
    daily_df = daily_df[
        daily_df["date"].dt.date.map(lambda d: calendar.is_trading_day(d))
    ].reset_index(drop=True)
    n_dropped = n_before - len(daily_df)
    if n_dropped:
        logger(f"[calendar] 过滤 {n_dropped} 条非交易日幻影行（{len(daily_df)}/{n_before} 保留）")
    if daily_df.empty:
        raise RuntimeError("过滤后增量帧为空 —— 拒绝落盘（门禁）")
    daily_by_code = {c: daily_df[daily_df["code"] == c] for c in rows_by_code}

    # ---- 4. 新鲜度门禁（date_max == target_day 才绿） ----
    level, gate = freshness_gate(daily_by_code, calendar, asof, allow_stale=allow_stale)
    logger(f"[gate] freshness={level} cutoff={gate['cutoff']} date_max={gate['date_max']}")
    for p in gate["problems"]:
        logger(f"  [gate] {p}")
    if level == "red" and not allow_stale:
        raise RuntimeError(f"[新鲜度门禁] ETF 增量 daily 落后/缺失，中止且不落盘：{gate['problems']}")

    # ---- 5. 写增量分片（幂等；窗口可能多天 -> 按天各写一个 _incr/YYYYMMDD 分片） ----
    written = []
    for day, g in daily_df.groupby(daily_df["date"].dt.date):
        p = ingest_daily_increment(g, day.strftime("%Y%m%d"), root, writer, pd)
        written.append(p)
        logger(f"[write] etf 增量 {len(g)} 行（{day}） -> {p}")

    # ---- 6. 重派生 weekly/monthly（覆盖物化 + manifest） ----
    derived = []
    for code in codes:
        for freq in DERIVED_FREQS:
            man = derive_period(code, freq, root, calendar, asof, writer, pd)
            derived.append(man)
            logger(f"[derive] {code} {freq}: {man['rows']} 根（partial={man['partial_bars']}）"
                   f" -> {man['path']}")

    # ---- 7. 顺手 expire 删旧（Q5） ----
    summary = {"pipeline": "etf_incr", "asof": asof.isoformat(), "writer": writer,
               "asset": ASSET, "target_day": target_day.isoformat(),
               "increment_codes": sorted(rows_by_code),
               "daily_rows": int(len(daily_df)),
               "rows_dropped_non_trading": n_dropped,
               "written_partitions": [os.path.relpath(p, root) for p in written],
               "freshness": {"level": level, **gate},
               "derived": [{k: m[k] for k in ("code", "freq", "rows", "partial_bars", "path")}
                           for m in derived],
               "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}
    if expire:
        _expire(root, target_day, writer, logger, summary)
    return summary


def _expire(root, target_day, writer, logger, summary: dict) -> None:
    """增量末尾顺手 expire 删旧（Q5）。etf 保留 2430 交易日，删整月目录、删前归档。"""
    from common.store.writer import expire_partitions
    rep = expire_partitions(ASSET, FQ, RETENTION_TRADE_DAYS, root=root, today=target_day,
                            dry_run=False, pd=_pd())
    summary["expire"] = {"removed": len(rep.get("removed", [])), "cutoff": rep.get("cutoff")}
    for r in rep.get("removed", []):
        logger(f"  [expire] 删除 {r['dir']}（2430 交易日窗口，last_day {r['last_day']} < cutoff）")


def _synthetic_rows(code: str, start: _dt.date, end: _dt.date) -> list:
    """离线自测：合成增量窗口 rows（仅工作日，确定性）。"""
    import pandas as pd
    days = [d.date() for d in pd.date_range(start, end, freq="B")]
    out = []
    base = 3000.0 + (hash(code) % 1000)
    px = base
    for j, d in enumerate(days):
        c = px * (1 + 0.001 * ((j % 3) - 1))
        out.append({"tradeDate": d.strftime("%Y%m%d"), "open": px, "high": max(px, c) * 1.002,
                    "low": min(px, c) * 0.998, "close": c,
                    "tradingVol": 1_000_000 + j * 1000,
                    "tradingValue": (1_000_000 + j * 1000) * 25.0 / 1e8})
        px = c
    return out


def write_runlog(summary: dict, root: str) -> str:
    d = os.path.join(root, "..", "state", "data", "runlog")
    d = os.path.normpath(d)
    if not os.path.isdir(os.path.dirname(d)):
        d = os.path.join(root, "state", "data", "runlog")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"etf_incr_{summary['asof']}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ETF 资产类日级增量（§2.4 data-etf.yml）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--codes", default=",".join(CSINDEX_CODES))
    ap.add_argument("--asof", default=None, help="数据基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--writer", default="data-etf-incr")
    ap.add_argument("--offline", action="store_true", help="合成 rows，不联网（自测）")
    ap.add_argument("--allow-stale", action="store_true", help="新鲜度红降级为告警")
    ap.add_argument("--no-expire", action="store_true", help="跳过顺手 expire 删旧")
    ap.add_argument("--no-runlog", action="store_true")
    args = ap.parse_args(argv)

    codes = tuple(c.strip() for c in args.codes.split(",") if c.strip())
    asof = _dt.date.fromisoformat(args.asof) if args.asof else _dt.date.today()
    summary = run(codes, root=args.data_root, asof=asof, writer=args.writer,
                  offline=args.offline, allow_stale=args.allow_stale,
                  expire=not args.no_expire)
    if not args.no_runlog:
        try:
            p = write_runlog(summary, args.data_root)
            print(f"[runlog] {p}")
        except Exception as e:  # noqa: BLE001
            print(f"[runlog] 写入失败（不阻断）：{e}")
    print(json.dumps({k: summary[k] for k in ("target_day", "daily_rows", "freshness")
                      if k in summary}, ensure_ascii=False, indent=2))
    if summary.get("skipped"):
        print(f"[✓] etf_incr done（幂等跳过：{summary['reason']}）")
    else:
        print(f"[✓] etf_incr done: {summary['daily_rows']} rows -> {summary['target_day']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
