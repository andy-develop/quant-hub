#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指数抓取编排（方案 §2.7 大盘多周期 + §4.2 data-index.yml 的 ①~⑤ 步）。

覆盖两个一等公民大盘：`sh000001` 上证综指、`sz399001` 深圳成指，全历史、不过期。
daily 是唯一权威抓取源；weekly/monthly 由 daily 确定性重采样、物化入库并标 `is_partial`。

运行序列（同一次运行内完成，杜绝 R17 派生滞后）：
  1. fetch   ifzq 分页回溯全史 daily（腾讯 web.ifzq，单次~800根）
  2. gate    新鲜度门禁：daily date_max 落后最近已收盘交易日 >0 → 红、中止、不落盘
  3. write   逐月 write_incremental + seal_partition → market/index/raw/year=/month= 封存分区
  4. derive  aggregate_index_frame 重采样 weekly/monthly（带 is_partial，用交易日历判定）
             → market/index/broad/<code>/<freq>/<freq>.parquet + 派生 manifest（derived_from.sha256）
  5. runlog  把抓取/门禁/派生摘要写 state/data/runlog（在数据仓侧）

只依赖 common/ 冻结 API：store.writer / store.reader / aggregate / calendar / datasource.vendor_tencent。

用法：
    # 真实抓取（数据仓 workflow 内，data/ 已挂载）：
    python -m tools.data_pipeline.index --data-root data --writer "data-index@run N"
    # 离线自测（合成 bars，不联网）：
    python -m tools.data_pipeline.index --offline --data-root /tmp/idx
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys

# 让 `python -m tools.data_pipeline.index` 与裸跑都能 import common
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BROAD = ("sh000001", "sz399001")           # ★ 一等公民大盘（schema.BROAD_INDEX_CODES）
GROUP = "broad"                            # market/index/<GROUP>/<code>/<freq>/
FQ = "raw"                                 # 指数无前复权问题，daily 存 raw
INDEX_START = {"sh000001": "1990-12-19", "sz399001": "1991-04-03"}
DERIVED_FREQS = ("weekly", "monthly")


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


# ---------------------------------------------------------------------------
# bars -> 契约 daily 帧
# ---------------------------------------------------------------------------
def bars_to_frame(bars_by_code: dict, pd=None):
    """{code: [[date,open,close,high,low,volume], ...]} -> 契约 daily 帧。

    ifzq bar 列序（与 quant-lab backfill_kline.py 同源，权威）：
        [0]=date [1]=open [2]=close [3]=high [4]=low [5]=volume
    ★ 指数无成交额 -> amount = NaN（§2.7：求和后仍 NaN，绝不得填 0，
      否则上层把 NaN 当"当天零成交"）。
    """
    pd = pd or _pd()
    rows = []
    for code, bars in bars_by_code.items():
        for b in bars:
            if not isinstance(b, (list, tuple)) or len(b) < 6:
                continue
            try:
                o, c, h, l = float(b[1]), float(b[2]), float(b[3]), float(b[4])
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            try:
                v = int(float(b[5])) if b[5] not in ("", None) else 0
            except (TypeError, ValueError):
                v = 0
            rows.append({"code": code, "date": pd.Timestamp(b[0]),
                         "open": o, "high": h, "low": l, "close": c,
                         "volume": v, "amount": float("nan")})
    cols = ["code", "date", "open", "high", "low", "close", "volume", "amount"]
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values(["code", "date"], kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 抓取（分页回溯全史）
# ---------------------------------------------------------------------------
def fetch_history(session, symbol: str, start: str, end: str, *, chunk: int = 800,
                  sleep: float = 0.25, logger=print) -> list:
    """ifzq 单次约 800 根上限 -> 从 end 往前分页回溯到 start，按日期升序去重。"""
    from common.datasource.vendor_tencent import fetch_ifzq_daily
    import time
    out: list = []
    seen: set = set()
    cur_end = end
    guard = 0
    while guard < 64:                     # 大盘 ~8700 日 / 800 ≈ 11 段，64 段足够 + 防死循环
        guard += 1
        bars = fetch_ifzq_daily(session, symbol, start, cur_end, limit=chunk)
        if not bars:
            break
        new = [b for b in bars if b[0] not in seen]
        for b in new:
            seen.add(b[0])
        out = new + out                   # bars 升序，往前拼
        oldest = bars[0][0]
        logger(f"  [ifzq] {symbol} 段 {guard}: +{len(new)} 根（最早 {oldest}，累计 {len(out)}）")
        if oldest <= start or len(bars) < chunk:
            break
        cur_end = (_dt.date.fromisoformat(oldest) - _dt.timedelta(days=1)).isoformat()
        time.sleep(sleep)
    out.sort(key=lambda b: b[0])
    return out


def make_session():
    import requests
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                    "AppleWebKit/537.36"})
    return s


# ---------------------------------------------------------------------------
# 新鲜度门禁（§2.7：大盘是三域共用输入，比个股更严）
# ---------------------------------------------------------------------------
def freshness_gate(daily_by_code: dict, calendar, asof, *, allow_stale: bool = False):
    """两个大盘的 daily date_max 必须 = 最近已收盘交易日，否则红。

    返回 (level, detail)。level='red' 且 not allow_stale -> 调用方应中止不落盘。
    """
    cutoff = calendar.closed_only_cutoff(asof)     # 最近"已走完"的交易日
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
    # 覆盖率：两个大盘必须同时在场（§2.7 门禁：缺任一 → 红）
    missing = [c for c in BROAD if c not in daily_by_code or daily_by_code[c] is None
               or len(daily_by_code[c]) == 0]
    if missing:
        problems.append(f"大盘序列缺失（不允许只有上证没有深成）: {missing}")
    level = "red" if problems else "green"
    return level, {"cutoff": cutoff.isoformat(), "date_max": detail, "problems": problems}


def pd_max_date(df):
    return df["date"].max()


# ---------------------------------------------------------------------------
# 写入：daily 逐月封存
# ---------------------------------------------------------------------------
def backfill_daily(df, root: str, writer: str, pd=None) -> int:
    """全历史 daily -> 逐月 write_incremental + seal_partition（只用公共 API）。

    得到 market/index/raw/year=YYYY/month=MM/batch=NN.parquet 封存分区。
    幂等：write_incremental 同月同分片行数一致即跳过；seal 重跑覆盖同分区（同数据同字节）。
    ★ 合并重建：写某月前先读该月已存在的封存行 + _incr 残留，与本次抓取行合并去重。
      否则不同 run 只抓各自 code 子集时（如只回补 sh000852），整月覆盖会抹掉该月
      其他 code 的行 —— 2026-09-14 起 data-index 只跑 BROAD 就把 09-12 回补的
      sh000852 全抹掉，verify R17 连续红了 5 天。
    """
    pd = pd or _pd()
    from common.store.writer import write_incremental, seal_partition
    from common.store.reader import read_partition_daily, clear_month_incr, normalize_code
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    # ★ 必须先归一 code：抓取帧带前缀（sh000001），封存分区是短码（000001）。
    #   不归一直接 drop_duplicates 会把"sh000001 vs 000001"当不同键 -> 同 (code,date)
    #   双份进封存（verify 不变量红，2026-09-21 恢复 run 实测踩中）。
    df["code"] = df["code"].map(normalize_code)
    months = sorted(set(zip(df["date"].dt.year.tolist(), df["date"].dt.month.tolist())))
    for (y, m) in months:
        g = df[(df["date"].dt.year == y) & (df["date"].dt.month == m)]
        if g.empty:
            continue
        existing = read_partition_daily("index", FQ, int(y), int(m), root=root, pd=pd)
        if existing is not None and not existing.empty:
            g = pd.concat([existing, g], ignore_index=True)
            g = g.drop_duplicates(subset=["code", "date"], keep="last")
        first = g["date"].min().strftime("%Y%m%d")
        clear_month_incr("index", FQ, int(y), int(m), root=root)
        write_incremental(g, "index", FQ, first, root=root, writer=writer,
                          allow_overwrite=True, pd=pd)
        seal_partition("index", FQ, int(y), int(m), root=root, writer=writer, pd=pd)
    # seal 删掉了 _incr 分片，但其 manifest 条目仍在 -> 清理，让 manifest 与磁盘一致
    from tools.data_pipeline import prune_manifest
    prune_manifest(root, "index", FQ)
    return len(months)


def ingest_daily_increment(df, day, root: str, writer: str, pd=None) -> str:
    """稳态：当日增量 write_incremental（append-only 日分片）。"""
    from common.store.writer import write_incremental
    return write_incremental(df, "index", FQ, day, root=root, writer=writer, pd=pd or _pd())


# ---------------------------------------------------------------------------
# 派生：weekly / monthly
# ---------------------------------------------------------------------------
def daily_fingerprint(daily_df, pd=None) -> str:
    """daily 帧的确定性指纹（用于派生一致性断言 derived_from.sha256）。

    对 (code,date,open,high,low,close,volume) 的规范序列化取 sha256；amount 全 NaN 不参与。
    daily 变了指纹就变 -> 周月K 必须重算（§2.7 门禁 / R17）。
    """
    pd = pd or _pd()
    d = daily_df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    cols = [c for c in ("code", "date", "open", "high", "low", "close", "volume") if c in d.columns]
    d = d[cols].sort_values(["code", "date"], kind="stable")
    blob = d.to_csv(index=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def derive_period(code: str, freq: str, root: str, calendar, asof, writer: str, pd=None) -> dict:
    """daily -> weekly/monthly 物化到 market/index/<GROUP>/<code>/<freq>/，写派生 manifest。"""
    pd = pd or _pd()
    from common.store.reader import load
    from common.aggregate import aggregate_index_frame, AGGREGATOR_VERSION

    daily = load(asset="index", fq=FQ, freq="daily", code=code, root=root)
    if daily is None or daily.empty:
        raise RuntimeError(f"no daily index for code={code} under {root}")

    agg = aggregate_index_frame(daily, code, freq, calendar=calendar, asof=asof, pd=pd)

    outdir = os.path.join(root, "market", "index", GROUP, code, freq)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"{freq}.parquet")
    tmp = out + ".tmp"
    agg.to_parquet(tmp, index=False)
    os.replace(tmp, out)

    fp = daily_fingerprint(daily, pd)
    man = {
        "asset": "index", "code": code, "freq": freq, "derived": True,
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
    with open(os.path.join(mdir, f"index_{GROUP}_{code}_{freq}.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    return man


# ---------------------------------------------------------------------------
# 编排主流程
# ---------------------------------------------------------------------------
def run(codes=BROAD, *, root="data", asof=None, writer="data-index", offline=False,
        allow_stale=False, calendar=None, logger=print) -> dict:
    pd = _pd()
    asof = asof or _dt.date.today()
    if calendar is None:
        from common.calendar import load_calendar
        calendar = load_calendar(root=root)

    # ---- 1. 取 daily bars ----
    if offline:
        bars_by_code = _synthetic_bars(codes, pd)
    else:
        s = make_session()
        end = asof.isoformat()
        bars_by_code = {}
        for code in codes:
            start = INDEX_START.get(code, "1990-01-01")
            logger(f"[fetch] {code} {start}..{end}")
            bars_by_code[code] = fetch_history(s, code, start, end, logger=logger)

    daily_df = bars_to_frame(bars_by_code, pd)
    if daily_df.empty:
        raise RuntimeError("抓取结果为空 —— 拒绝落盘（§2.7 门禁）")
    daily_by_code = {c: daily_df[daily_df["code"] == c] for c in codes}

    # ---- 2. 新鲜度门禁 ----
    level, gate = freshness_gate(daily_by_code, calendar, asof, allow_stale=allow_stale)
    logger(f"[gate] freshness={level} cutoff={gate['cutoff']} date_max={gate['date_max']}")
    if gate["problems"]:
        for p in gate["problems"]:
            logger(f"  [gate] {p}")
    if level == "red" and not allow_stale:
        raise RuntimeError(f"[§2.7 新鲜度门禁] 大盘 daily 落后/缺失，中止且不落盘：{gate['problems']}")

    # ---- 3. 写 daily 封存分区 ----
    n_months = backfill_daily(daily_df, root, writer, pd)
    logger(f"[write] daily 封存 {n_months} 个月分区 -> market/index/{FQ}/year=*/month=*")

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
        "pipeline": "index", "asof": asof.isoformat(), "writer": writer,
        "codes": list(codes), "daily_rows": int(len(daily_df)), "months_sealed": n_months,
        "freshness": {"level": level, **gate},
        "derived": [{k: m[k] for k in ("code", "freq", "rows", "partial_bars", "path")}
                    for m in derived],
        "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
    }
    return summary


def _synthetic_bars(codes, pd):
    """离线自测：造 2026-07-01..09-11 的确定性病合成 bars（不联网）。"""
    out = {}
    days = [d for d in pd.date_range("2026-07-01", "2026-09-11", freq="B")]
    for i, code in enumerate(codes):
        base = 3000.0 + i * 1000
        bars = []
        px = base
        for j, d in enumerate(days):
            o = px
            c = px * (1 + 0.001 * ((j % 5) - 2))
            h = max(o, c) * 1.002
            l = min(o, c) * 0.998
            v = 1_000_000 + j * 1000
            bars.append([d.strftime("%Y-%m-%d"), round(o, 2), round(c, 2),
                         round(h, 2), round(l, 2), v])
            px = c
        out[code] = bars
    return out


def write_runlog(summary: dict, root: str) -> str:
    d = os.path.join(root, "..", "state", "data", "runlog")
    # 数据仓内 runlog 落在 data 仓根的 state/（与 data/ 同级）；无则落 root/state
    d = os.path.normpath(d)
    if not os.path.isdir(os.path.dirname(d)):
        d = os.path.join(root, "state", "data", "runlog")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"index_{summary['asof']}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="指数抓取编排（§2.7）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--codes", default=",".join(BROAD))
    ap.add_argument("--asof", default=None, help="数据基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--writer", default="data-index")
    ap.add_argument("--offline", action="store_true", help="合成 bars，不联网（自测）")
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
    print(f"[✓] index pipeline done: {summary['daily_rows']} daily rows, "
          f"{len(summary['derived'])} derived partitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
