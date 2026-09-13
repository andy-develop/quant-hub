#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个股日级增量（方案 §2.4 data-stock.yml 的 16:40 步，阶段 B）。

对标老仓 quant-lab scripts/daily_update.py 的成熟机制（hfq 后复权冻结 + 除权检测 + 折算），
全部换成契约 writer 与 common/datasource 编排，产出 market/stock/{raw,hfq}/_incr/YYYYMMDD/：

  1. gate    目标日 = 最近已收盘交易日（calendar.closed_only_cutoff），非交易日自动跳过
  2. prev    库内最后一条 raw/hfq close（严格 < 目标日）→ 除权检测基准 + hfq 折算因子
  3. fetch   腾讯批量快照全A在市（TencentSnapshotVendor，60 只/批，TokenBucket 1/s）
  4. div     除权检测：快照昨收 vs 库内上一收盘 |Δ|>0.5% → ifzq 整段重拉修复
             raw 修复 = to_contract_raw(ifzq 整段)（volume 手→股，与迁移同口径）
             hfq 修复 = qfq × K（K = 库内最后 hfq close / 重拉 qfq 同日 close，后复权锚定）
  5. frame   raw 契约帧：volume_股 = round(amount/close)（与 stock_migrate 迁移逐位同口径）
             hfq 帧 = raw OHLC × ratio（ratio = 库内 hfq_last/raw_last；除权股用修复序列），
             volume/amount 沿用 raw 真实值（量额与复权无关）
  6. write   write_incremental 幂等落盘；除权股写 market/stock/fixup/{raw,hfq}_{code}.parquet
  7. expire  顺手 expire_partitions(asset="stock", 730 交易日) 删旧（grill-me Q5：日级增量顺手删）
  8. runlog  抓取/除权/写盘摘要 → state/data/runlog/stock_<asof>.json

★ 与老仓保持逐位同口径的两个地方（决定阶段 F shadow_diff 能否通过）：
  - raw 的 volume/amount：与 stock_migrate.to_contract_raw 完全一致（amount 元 / volume 股）
  - hfq 折算：ratio 与除权修复 K 的定义逐字复刻 daily_update.py（后复权冻结）

用法：
    # 真实抓取（数据仓 workflow 内，data/ 已挂载）：
    python -m tools.data_pipeline.stock_incr --data-root data --writer "data-stock-incr@run N" --expire
    # 离线自测（合成快照，不联网；div 股跳过修复）：
    python -m tools.data_pipeline.stock_incr --offline --data-root /tmp/stock
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ASSET = "stock"
FQS = ("raw", "hfq")
DIV_TOLERANCE = 0.005          # 快照昨收 vs 库内上一收盘 偏差容差（老仓同款 0.5%）
DIV_FUSE_MAX = 50              # 除权股数量保险丝下界（老仓同款 max(50, 0.3*n)）
COVERAGE_RED = 0.80            # §0.4 覆盖率门禁：<80% 红
COVERAGE_YELLOW = 0.95         # <95% 黄（记录，不阻断）
REFETCH_START_DELTA = 4        # 除权修复窗口起点 = 目标日 - 4 年（覆盖库内 730 交易日窗口）


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


# ---------------------------------------------------------------------------
# 1. 库内最新收盘（除权检测基准 + hfq 折算因子）
# ---------------------------------------------------------------------------
def latest_closes(root: str, asset: str, fq: str, *, before=None, pd=None) -> dict:
    """读「最新封存月 + 全部 _incr 日分片」，返回 {code: (date, close)}。

    为什么够：hfq/raw 历史冻结 + 增量只 append，最新一行必然落在（最新封存月）或
    （_incr）。只读这两处，避免每天全量扫 730 交易日（405 万行）的浪费。
    """
    pd = pd or _pd()
    from common.store.reader import normalize_code
    base = os.path.join(root, "market", asset, fq)
    files: list[str] = []
    months = sorted(glob.glob(os.path.join(base, "year=*", "month=*")))
    if months:
        files += sorted(glob.glob(os.path.join(months[-1], "*.parquet")))   # 最新封存月
    inc = os.path.join(base, "_incr")
    if os.path.isdir(inc):
        files += sorted(glob.glob(os.path.join(inc, "*", "*.parquet")))     # 全部未封存日分片
    if not files:
        return {}
    dfs = []
    for p in files:
        try:
            dfs.append(pd.read_parquet(p, columns=["code", "date", "close"]))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] latest_closes 跳过 {p}: {e}")
    if not dfs:
        return {}
    df = pd.concat(dfs, ignore_index=True)
    df["code"] = df["code"].map(normalize_code)
    df["date"] = pd.to_datetime(df["date"])
    if before is not None:
        df = df[df["date"] < pd.Timestamp(before)]
    if df.empty:
        return {}
    last = df.sort_values("date").groupby("code").tail(1)
    return {c: (d, float(cl)) for c, d, cl in zip(last["code"], last["date"], last["close"])}


def build_ratio(raw_last: dict, hfq_last: dict) -> dict:
    """ratio = 库内最后 hfq_close / raw_close（后复权因子，非除权日恒定；老仓 join inner 同款）。"""
    out = {}
    for c in set(raw_last) & set(hfq_last):
        _, rc = raw_last[c]
        _, hc = hfq_last[c]
        if rc and rc > 0:
            out[c] = hc / rc
    return out


# ---------------------------------------------------------------------------
# 2. 抓取：腾讯批量快照全A
# ---------------------------------------------------------------------------
def fetch_snapshot(secids: list[str], *, logger=print):
    """腾讯批量快照全A（在市）。返回 (rows, stats)。

    rows 每条的 code 是腾讯 secid（1.600004）、date=None —— ★快照不含日期，
    由调用方统一填 target_day（vendor_tencent.parse_qt_batch_response 契约）。
    """
    from common.datasource import fetch_with_fallback, FetchStats
    from common.datasource.vendor_tencent import TencentSnapshotVendor
    vendor = TencentSnapshotVendor(logger=logger).to_vendor(secids)
    stats = FetchStats(vendor=vendor.name)
    rows, stats = fetch_with_fallback([vendor], list(secids), batch_size=60,
                                      stats=stats, logger=logger)
    return rows, stats


# ---------------------------------------------------------------------------
# 3. 除权检测 + ifzq 整段修复（hfq 后复权锚定）
# ---------------------------------------------------------------------------
def detect_dividends(snap, prev_raw: dict, *, logger=print) -> list[str]:
    """快照昨收 vs 库内上一收盘 |Δ|>0.5% → div；超保险丝（>max(50, 0.3n)）中止。"""
    div = []
    for r in snap:
        code = r["code"]
        prev = prev_raw.get(code)
        pc = r.get("prev_close")
        if prev is None or not pc:
            continue
        if abs(pc / prev - 1.0) > DIV_TOLERANCE:
            div.append(code)
    fuse = max(DIV_FUSE_MAX, int(0.3 * len(snap)))
    if len(div) > fuse:
        raise RuntimeError(
            f"除权检测异常: {len(div)}/{len(snap)} 只被标记 (>fuse={fuse}) —— "
            f"大概率交易日错位假阳性，中止以免整段重拉打爆数据源限流（§0.4 教训）")
    return div


def _ifzq_paged(session, sym: str, start: str, end: str, fq: str = "", *, logger=print) -> list:
    """ifzq 日K分页回溯（单次 ~800 根上限），按日期升序去重。"""
    import time
    from common.datasource.vendor_tencent import fetch_ifzq_daily
    out: list = []
    seen: set = set()
    cur = end
    guard = 0
    while guard < 32:
        guard += 1
        bars = fetch_ifzq_daily(session, sym, start, cur, fq=fq, limit=800)
        if not bars:
            break
        new = [b for b in bars if b[0] not in seen]
        for b in new:
            seen.add(b[0])
        out = new + out
        if bars[0][0] <= start or len(bars) < 800:
            break
        cur = (_dt.date.fromisoformat(bars[0][0]) - _dt.timedelta(days=1)).isoformat()
        time.sleep(0.25)
    out.sort(key=lambda b: b[0])
    return out


def _bars_to_frame(bars: list, code: str, pd) -> "pd.DataFrame":
    """ifzq bar -> 含 code/date/OHLC/volume 的帧（volume 单位=手，随后 to_contract_raw 换算）。"""
    df = pd.DataFrame([b[:6] for b in bars],
                      columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    df.insert(0, "code", code)
    return df


def refetch_fixup(session, code: str, sym: str, target: _dt.date, *, pd=None, logger=print):
    """除权股整段重拉：raw = ifzq 不复权（契约帧）；qfq = ifzq 前复权（供 hfq 锚定）。"""
    pd = pd or _pd()
    from tools.data_pipeline.stock_migrate import to_contract_raw
    end = target.isoformat()
    start = (target - _dt.timedelta(days=365 * REFETCH_START_DELTA)).isoformat()
    logger(f"  [div] {code} 整段重拉 {start}..{end}")
    raw_bars = _ifzq_paged(session, sym, start, end, fq="", logger=logger)
    qfq_bars = _ifzq_paged(session, sym, start, end, fq="qfq", logger=logger)
    if not raw_bars or not qfq_bars:
        return None
    raw_df = _bars_to_frame(raw_bars, code, pd)
    qfq_df = _bars_to_frame(qfq_bars, code, pd)
    if raw_df.empty or qfq_df.empty:
        return None
    raw_ct = to_contract_raw(raw_df, pd)          # volume 手→股 / amount 兜底，与迁移同口径
    if raw_ct.empty:
        return None
    return raw_ct, qfq_df


def derive_hfq_fixup(code: str, qfq_df, hfq_anchor, raw_ct, *, pd=None):
    """hfq 修复帧 = qfq OHLC × K，volume/amount 从 raw_ct 并入。

    K = 库内最后 hfq_close / 重拉 qfq 同日 close（老仓 derive_hfq_fixup 逐字同款；
    后复权冻结：锚定历史最后一条，整段乘常数 K，不与最新价整体缩放）。
    """
    pd = pd or _pd()
    if hfq_anchor is None:
        return None
    anchor_date, anchor_close = hfq_anchor
    q = qfq_df.copy()
    q["date"] = pd.to_datetime(q["date"])
    anchor = q[q["date"] == pd.Timestamp(anchor_date)]
    if anchor.empty or not anchor_close:
        return None
    k = anchor_close / float(anchor["close"].iloc[0])
    out = q[["code", "date", "open", "close", "high", "low"]].copy()
    for c in ("open", "close", "high", "low"):
        out[c] = pd.to_numeric(out[c], errors="coerce") * k
    # volume/amount 用真实量额（与复权无关）—— 从 raw 修复帧并入（老仓 attach_real_vol_amount）
    r = raw_ct[["code", "date", "volume", "amount"]].copy()
    out = out.merge(r, on=["code", "date"], how="left")
    out["volume"] = out["volume"].fillna(0).astype("int64")
    out["amount"] = out["amount"].fillna(0.0).astype("float64")
    return out[["code", "date", "open", "high", "low", "close", "volume", "amount"]]


# ---------------------------------------------------------------------------
# 4. 契约帧
# ---------------------------------------------------------------------------
def frame_raw(snap, target_day, pd=None):
    """快照行 -> raw 契约帧（volume 股 = round(amount/close)，与迁移 to_contract_raw 同口径）。

    复用 stock_migrate.to_contract_raw：快照 volume 单位=手、amount=真值(元)，
    fallback 分支与迁移逐位一致。date 统一填 target_day（快照不含日期）。
    """
    pd = pd or _pd()
    from tools.data_pipeline.stock_migrate import to_contract_raw
    d = pd.DataFrame(snap)
    d["date"] = pd.Timestamp(target_day)
    return to_contract_raw(d, pd)


def frame_hfq(raw_frame, ratio: dict, div_fix: dict, pd=None):
    """hfq 帧 = raw OHLC × ratio；除权股当日行用修复序列（div_fix[code]=(raw_ct, hfq_ct)）。"""
    pd = pd or _pd()
    out = raw_frame.copy()
    adj = out["code"].map(ratio).fillna(1.0)
    for c in ("open", "high", "low", "close"):
        out[c] = pd.to_numeric(out[c], errors="coerce") * adj
    for code, (_, hfq_ct) in div_fix.items():
        if hfq_ct is None or hfq_ct.empty:
            continue
        day = hfq_ct["date"].max()
        row = hfq_ct[hfq_ct["date"] == day]
        if row.empty:
            continue
        out = out[out["code"] != code]
        out = pd.concat([out, row[["code", "date", "open", "high", "low", "close", "volume", "amount"]]],
                        ignore_index=True)
    return out


# ---------------------------------------------------------------------------
# 5. 硬校验（G5）
# ---------------------------------------------------------------------------
def validate_increment(df, target_day, pd) -> None:
    from common.store.reader import StoreError
    if df.empty:
        raise StoreError("增量帧为空 —— 拒绝落盘")
    if df["close"].isna().any() or not (df["close"] > 0).all():
        raise StoreError("G5: close 存在 NaN/<=0 行")
    bad_day = df[df["date"].dt.date != pd.Timestamp(target_day).date()]
    if len(bad_day):
        raise StoreError(f"G5: {len(bad_day)} 行日期 != 目标日 {target_day}")
    dup = df.duplicated(subset=["code", "date"]).sum()
    if dup:
        raise StoreError(f"G5: {dup} 个重复 (code,date)")


# ---------------------------------------------------------------------------
# 6. 编排主流程
# ---------------------------------------------------------------------------
def run(*, root="data", asof=None, writer="data-stock-incr", offline=False,
        expire=False, calendar=None, logger=print) -> dict:
    pd = _pd()
    asof = asof or _dt.date.today()
    if calendar is None:
        from common.calendar import load_calendar
        calendar = load_calendar(root=root)
    target_day = calendar.closed_only_cutoff(asof)      # 最近已收盘交易日
    logger(f"[gate] asof={asof} target_day={target_day}（最近已收盘交易日）")

    # ---- 幂等：同日分片已存在 -> 跳过抓取/写入（write_incremental 原子写） ----
    inc = os.path.join(root, "market", ASSET, "raw", "_incr", target_day.strftime("%Y%m%d"), "raw.parquet")
    if os.path.exists(inc):
        logger(f"[skip] {target_day} raw 增量已入库（{inc}），跳过抓取与写入")
        summary = {"pipeline": "stock_incr", "asof": asof.isoformat(), "writer": writer,
                   "asset": ASSET, "target_day": target_day.isoformat(),
                   "skipped": True, "reason": "increment exists",
                   "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}
        if expire:
            _expire(root, target_day, writer, logger, summary)
        return summary

    # ---- 1. universe（在市） ----
    from common.store.reader import load_meta, normalize_code
    uni = load_meta("universe", root=root)
    uni = uni[uni["status"].astype(str) == "1"]
    secids = [str(s) for s in uni["secid"].tolist()]
    total = len(secids)
    logger(f"[universe] 在市 {total} 只（快照目标）")

    # ---- 2. 库内最新收盘（除权基准 + hfq 因子） ----
    raw_last = {} if offline else latest_closes(root, ASSET, "raw", before=target_day, pd=pd)
    hfq_last = {} if offline else latest_closes(root, ASSET, "hfq", before=target_day, pd=pd)
    ratio = build_ratio(raw_last, hfq_last)
    logger(f"[prev] raw_last {len(raw_last)} 只 · hfq_last {len(hfq_last)} 只 · ratio {len(ratio)} 只")

    # ---- 3. 抓快照 ----
    if offline:
        snap = _synthetic_snap(pd, total, target_day)
        for r in snap:
            r["code"] = normalize_code(r["code"])
    else:
        rows, stats = fetch_snapshot(secids, logger=logger)
        logger(f"[fetch] {stats.ok}/{stats.requested} 只 · dropped={stats.dropped_total} · "
               f"missing={len(stats.missing_codes)}")
        for c in stats.missing_codes[:10]:
            logger(f"  [fetch] missing: {c}")
        snap = [{"code": normalize_code(r["code"]), "date": None, "open": r["open"],
                 "close": r["close"], "high": r["high"], "low": r["low"],
                 "volume": r["volume"], "amount": r["amount"],
                 "prev_close": r.get("prev_close")} for r in rows]
    if not snap:
        raise RuntimeError("快照为空 —— 拒绝落盘（门禁）")

    # ---- 覆盖率门禁（§0.4：<80% 红 / <95% 黄） ----
    got_codes = {r["code"] for r in snap}
    coverage = len(got_codes) / total
    logger(f"[coverage] {len(got_codes)}/{total} = {coverage:.1%}")
    if coverage < COVERAGE_RED:
        raise RuntimeError(f"覆盖率 {coverage:.1%} < 80% —— 沪市覆盖故障（§0.4）级别问题，中止不落盘")
    if coverage < COVERAGE_YELLOW:
        logger(f"  [coverage] ⚠ 黄：覆盖率 {coverage:.1%} < 95%，missing_codes 留待补跑")

    # ---- 4. 除权检测 + 修复 ----
    div_codes = ([] if offline else detect_dividends(snap, raw_last, logger=logger))
    if offline:
        div_codes = _synthetic_div_codes(snap)
    logger(f"[div] 检测到除权 {len(div_codes)} 只")
    div_fix: dict = {}
    div_failed: list[str] = []
    if div_codes and not offline:
        import requests
        from common.store.reader import code_to_symbol
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                        "AppleWebKit/537.36"})
        for code in div_codes:
            try:
                res = refetch_fixup(s, code, code_to_symbol(code), target_day, pd=pd, logger=logger)
                if res is None:
                    div_failed.append(code)
                    logger(f"  [div] {code} 修复失败（重拉为空），留待补跑")
                    continue
                raw_ct, qfq_df = res
                if raw_ct["date"].max().date() != target_day:
                    div_failed.append(code)
                    logger(f"  [div] {code} 修复序列未到目标日（{raw_ct['date'].max().date()}），留待补跑")
                    continue
                hfq_ct = derive_hfq_fixup(code, qfq_df, hfq_last.get(code), raw_ct, pd=pd)
                if hfq_ct is None or hfq_ct["date"].max().date() != target_day:
                    div_failed.append(code)
                    logger(f"  [div] {code} hfq 锚定/补齐失败，留待补跑")
                    continue
                div_fix[code] = (raw_ct, hfq_ct)
                logger(f"  [div] {code} 修复完成 raw {len(raw_ct)} 行 / hfq {len(hfq_ct)} 行")
            except Exception as e:  # noqa: BLE001
                div_failed.append(code)
                logger(f"  [div] {code} 修复异常: {e}")
    if div_failed:
        logger(f"  [div] 修复失败 {len(div_failed)} 只（留待补跑 cron 补齐）: {div_failed[:20]}")
    if div_codes and offline:
        logger(f"  [div] offline 模式跳过修复（不联网），{len(div_codes)} 只不落盘当日行")

    # ---- 5. 契约帧 + 除权股当日行 ----
    snap_ok = [r for r in snap if r["code"] not in div_codes]
    raw_frame = frame_raw(snap_ok, target_day, pd)
    if div_fix:
        for code, (raw_ct, _) in div_fix.items():
            day = raw_ct["date"].max()
            row = raw_ct[raw_ct["date"] == day]
            if row.empty:
                continue
            raw_frame = raw_frame[raw_frame["code"] != code]
            raw_frame = pd.concat([raw_frame, row], ignore_index=True)
    hfq_frame = frame_hfq(raw_frame, ratio, div_fix, pd)

    # ---- G5 硬校验 ----
    validate_increment(raw_frame, target_day, pd)
    validate_increment(hfq_frame, target_day, pd)

    # ---- 6. 写盘 ----
    from common.store.writer import write_incremental
    day_str = target_day.strftime("%Y%m%d")
    p_raw = write_incremental(raw_frame, ASSET, "raw", day_str, root=root, writer=writer, pd=pd)
    p_hfq = write_incremental(hfq_frame, ASSET, "hfq", day_str, root=root, writer=writer, pd=pd)
    logger(f"[write] raw {len(raw_frame):,} 行 -> {p_raw}")
    logger(f"[write] hfq {len(hfq_frame):,} 行 -> {p_hfq}")

    # 除权股 fixup 落盘（reader._normalize_frame 按 code 整只覆盖）
    fix_written = []
    if div_fix:
        fdir = os.path.join(root, "market", ASSET, "fixup")
        os.makedirs(fdir, exist_ok=True)
        for code, (raw_ct, hfq_ct) in div_fix.items():
            for fq, df in (("raw", raw_ct), ("hfq", hfq_ct)):
                p = os.path.join(fdir, f"{fq}_{code}.parquet")
                tmp = p + ".tmp"
                df.to_parquet(tmp, index=False)
                os.replace(tmp, p)
                fix_written.append(os.path.relpath(p, root))
        logger(f"[fixup] 写入 {len(fix_written)} 个修复分区: {fix_written}")

    # ---- 7. 顺手 expire 删旧（Q5） ----
    summary = {"pipeline": "stock_incr", "asof": asof.isoformat(), "writer": writer,
               "asset": ASSET, "target_day": target_day.isoformat(),
               "universe_in_market": total, "snapshot_ok": len(snap),
               "coverage": round(coverage, 4),
               "div_codes": div_codes, "div_failed": div_failed,
               "fixup_partitions": fix_written,
               "raw_rows": int(len(raw_frame)), "hfq_rows": int(len(hfq_frame)),
               "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")}
    if expire:
        _expire(root, target_day, writer, logger, summary)
    return summary


def _expire(root, target_day, writer, logger, summary: dict) -> None:
    """增量末尾顺手 expire 删旧（Q5）。stock 保留 730 交易日，删整月目录、删前归档。"""
    from common.store.writer import expire_partitions
    reports = {}
    for fq in FQS:
        rep = expire_partitions(ASSET, fq, 730, root=root, today=target_day,
                                dry_run=False, pd=_pd())
        reports[fq] = {"removed": len(rep.get("removed", [])), "cutoff": rep.get("cutoff")}
        for r in rep.get("removed", []):
            logger(f"  [expire] 删除 {r['dir']}（730 交易日窗口，last_day {r['last_day']} < cutoff）")
    summary["expire"] = reports


# ---------------------------------------------------------------------------
# offline 合成（自测，不联网）
# ---------------------------------------------------------------------------
def _synthetic_snap(pd, n: int, target: _dt.date) -> list[dict]:
    rows = []
    for i in range(n):
        base = 10.0 + i * 0.5
        prev = base * (1.02 if i % 11 == 0 else 1.0)     # 每 11 只造 1 只除权（昨收偏差 2%）
        o = c = prev
        rows.append({"code": f"{600000 + i}", "date": pd.Timestamp(target), "open": o,
                     "high": prev * 1.01, "low": prev * 0.99, "close": c,
                     "volume": 10000 + i * 100, "amount": (10000 + i * 100) * prev * 100,
                     "prev_close": base})
    return rows


def _synthetic_div_codes(snap) -> list[str]:
    """offline 下与 _synthetic_snap 的除权标记对应（i%11==0）。"""
    return [r["code"] for i, r in enumerate(snap) if i % 11 == 0]


# ---------------------------------------------------------------------------
def write_runlog(summary: dict, root: str) -> str:
    d = os.path.join(root, "..", "state", "data", "runlog")
    d = os.path.normpath(d)
    if not os.path.isdir(os.path.dirname(d)):
        d = os.path.join(root, "state", "data", "runlog")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"stock_{summary['asof']}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="个股日级增量（§2.4 data-stock.yml）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--asof", default=None, help="数据基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--writer", default="data-stock-incr")
    ap.add_argument("--offline", action="store_true", help="合成快照，不联网（自测）")
    ap.add_argument("--expire", action="store_true", help="增量后顺手 expire 删旧（Q5；workflow 传入）")
    ap.add_argument("--no-runlog", action="store_true")
    args = ap.parse_args(argv)

    asof = _dt.date.fromisoformat(args.asof) if args.asof else _dt.date.today()
    summary = run(root=args.data_root, asof=asof, writer=args.writer,
                  offline=args.offline, expire=args.expire)
    if not args.no_runlog:
        try:
            p = write_runlog(summary, args.data_root)
            print(f"[runlog] {p}")
        except Exception as e:  # noqa: BLE001
            print(f"[runlog] 写入失败（不阻断）：{e}")
    print(json.dumps({k: summary[k] for k in ("target_day", "snapshot_ok", "coverage",
                                              "div_codes", "raw_rows", "hfq_rows")
                      if k in summary}, ensure_ascii=False, indent=2))
    if summary.get("skipped"):
        print(f"[✓] stock_incr done（幂等跳过：{summary['reason']}）")
    else:
        print(f"[✓] stock_incr done: {summary['raw_rows']:,} raw / {summary['hfq_rows']:,} hfq rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
