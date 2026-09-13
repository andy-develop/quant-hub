#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""短线域自产（方案 §4.2 strategy-pm / §7.1）：跑 quant-lab 的计算链产出真实 payload。

quant-lab 的 daily.yml 已 commit `data/kline` + `data/meta/{stock_basic,index_daily,
bench_daily,st_history,lgbm_model}`，所以**计算链可离线跑**（不联网抓取）：
    signals.run_scan → engine.run_backtest(开/关) → [blackbox] → build_report.main → report/index.html
再从 report/index.html 抽 `const MODES = {...}` / `const MODES_BB = {...}`，规范化为统一信封。

★ 零改动 quant-lab 策略代码：只 import 它的模块按原顺序跑（与 run_daily.py 同款），
  跳过 daily_update（抓取，属数据链路职责）。

用法（CI 内，quant-lab 检出在 --qlab）：
    python -m tools.run_shortterm --qlab src/quant-lab --out state/payload [--skip-blackbox]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _extract_modes(html: str):
    """从 report/index.html 抽 MODES / MODES_BB（build_report.render_html 注入的 JSON）。"""
    def grab(var, nxt):
        # const MODES = {...};\nconst MODES_BB = ...  -> 取到下一个 const 之前
        m = re.search(rf'const {var}\s*=\s*(\{{.*?\}})\s*;\s*\nconst {nxt}', html, re.S)
        if m:
            return json.loads(m.group(1))
        m = re.search(rf'const {var}\s*=\s*(\{{.*\}})\s*;', html, re.S)
        return json.loads(m.group(1)) if m else None
    modes = grab("MODES", "MODES_BB")
    # MODES_BB 是最后一个，取到行尾的 };
    mbb = re.search(r'const MODES_BB\s*=\s*(\{.*?\})\s*;\s*\n', html, re.S)
    modes_bb = json.loads(mbb.group(1)) if mbb else None
    return modes, modes_bb


def _build_summary(payloads, data_date, has_blackbox, *, writer=None):
    """从统一信封抽回测摘要（Q3：摘要入 state/）。

    KPI 口径与 tools/shadow_diff.py 逐位一致（momentum/blackbox × on/off 各 6 项：
    ret/mdd/sharpe/n_trades/win_rate/last_equity），保证阶段 F 比对不漂移。
    """
    from tools.shadow_diff import _shortterm_line_kpis

    by = {(env.get("domain"), env.get("variant")): env for env in (payloads or [])}
    kpi_names = ("ret", "mdd", "sharpe", "n_trades", "win_rate", "last_equity")
    out = {
        "domain": "shortterm",
        "generated_at": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "data_date": data_date,
        "has_blackbox": has_blackbox,
        "writer": writer,
        "kpis": {},
    }
    for variant in ("momentum", "blackbox"):
        env = by.get(("quant-lab", variant))
        y3 = (env.get("payload") or {}).get("y3") if env else None
        lines = {}
        for line in ("on", "off"):
            node = (y3 or {}).get(line)
            vals = _shortterm_line_kpis(node) if node else [0.0] * 6
            lines[line] = dict(zip(kpi_names, vals))
        out["kpis"][variant] = lines
    return out


def _write_summary(summary: dict, out: str, logger=print) -> str:
    """写摘要 JSON（入 git，幂等覆盖）。out 为空则不写。"""
    if not out:
        return ""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out)
    logger(f"[summary] {out}")
    return out


def run(qlab: str, out: str, *, skip_blackbox: bool = False, logger=print,
        summary_out: str | None = None, detail_out: str | None = None,
        writer: str | None = None) -> dict:
    scripts = os.path.join(qlab, "scripts")
    if not os.path.isdir(scripts):
        raise RuntimeError(f"quant-lab 检出无效（无 scripts/）：{qlab}")
    sys.path.insert(0, scripts)

    import signals          # noqa: E402
    import engine           # noqa: E402
    import build_report     # noqa: E402
    BASE = engine.BASE      # = qlab 根（模块按 __file__ 定位）

    logger("[1/4] signals.run_scan（动量信号扫描）")
    signals.run_scan()

    logger("[2/4] engine.run_backtest（开/关仓位双口径）")
    engine.run_backtest(use_regime=True, out_dir=f"{BASE}/data/meta")
    engine.run_backtest(use_regime=False, out_dir=f"{BASE}/data/meta_no")

    if not skip_blackbox:
        try:
            logger("[3/4] 量化黑盒（LightGBM walk-forward，可选）")
            import lgbm_rank
            lgbm_rank.update_scores()
            sig_bb = f"{BASE}/data/meta/signals_bb.parquet"
            signals.run_scan(score_mode="lgbm", out_file=sig_bb, strat="blackbox")
            engine.run_backtest(use_regime=True, out_dir=f"{BASE}/data/meta_bb", sig_file=sig_bb)
            engine.run_backtest(use_regime=False, out_dir=f"{BASE}/data/meta_no_bb", sig_file=sig_bb)
        except Exception as e:  # noqa: BLE001
            logger(f"[3/4] 黑盒跳过（不阻塞动量主线）：{str(e)[:200]}")
    else:
        logger("[3/4] 黑盒已跳过（--skip-blackbox）")

    logger("[4/4] build_report.main → report/index.html")
    build_report.main()

    report = f"{BASE}/report/index.html"
    if not os.path.exists(report):
        raise RuntimeError(f"报告未生成：{report}")
    html = open(report, encoding="utf-8").read()
    modes, modes_bb = _extract_modes(html)
    if not modes:
        raise RuntimeError("未能从 report/index.html 抽出 MODES（格式变了？查 build_report.render_html）")

    from common.payload.adapters import normalize_quant_lab, write_envelopes
    # data_date：取 report 里 MODES 的最新日期（若有），否则留空由前端处理
    data_date = None
    try:
        dates = (modes.get("y3", {}).get("on", {}) or {}).get("dates")
        if dates:
            data_date = str(dates[-1])[:10]
    except Exception:  # noqa: BLE001
        pass
    payloads = normalize_quant_lab(modes=modes, modes_bb=modes_bb, data_date=data_date)
    paths = write_envelopes(payloads, out)
    logger(f"[✓] 短线自产 {len(paths)} 个 payload -> {out}（data_date={data_date}）")

    # ---- Q3：摘要入 state/（git）+ 明细落数据仓 ----
    summary = _build_summary(payloads, data_date, bool(modes_bb), writer=writer)
    s_path = _write_summary(summary, summary_out or "", logger)
    n_detail = _export_detail(BASE, detail_out or "", logger)

    return {"payloads": len(paths), "data_date": data_date,
            "has_blackbox": bool(modes_bb), "summary": s_path or None,
            "detail_files": n_detail}


# 回测核心明细（日快照入数据仓 data/state/shortterm/backtest/YYYY-MM-DD/）。
# ★ 排除 flags_long.parquet（65M 重算可得）与 lgbm_scores.parquet（9.9M 黑盒分数）——
#   明细只留"给定 data/+sha 可逐位重算"的判决性输出。
DETAIL_FILES = ("equity.csv", "trades.parquet", "holdings.parquet",
                "signals.parquet", "market_regime.parquet", "signals_bb.parquet")


def _export_detail(base: str, detail_out: str, logger=print) -> int:
    """把 qlab data/meta 的核心回测明细复制到 detail_out（不存在则跳过）。"""
    if not detail_out:
        return 0
    import shutil
    os.makedirs(detail_out, exist_ok=True)
    n = 0
    for f in DETAIL_FILES:
        src = os.path.join(base, "data", "meta", f)
        if not os.path.exists(src):
            continue
        dst = os.path.join(detail_out, f)
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        n += 1
    logger(f"[detail] 导出 {n} 个回测明细 -> {detail_out}")
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="短线域自产（跑 quant-lab 计算链）")
    ap.add_argument("--qlab", required=True, help="quant-lab 检出根（含 data/kline + scripts）")
    ap.add_argument("--out", default="state/payload")
    ap.add_argument("--skip-blackbox", action="store_true", help="跳过 LightGBM 黑盒（省时/无 lightgbm 时）")
    ap.add_argument("--summary-out", default="state/shortterm/backtest_summary.json",
                    help="回测摘要（Q3 入 git；空串=不写）")
    ap.add_argument("--detail-out", default="",
                    help="回测明细日快照目录（Q3 入数据仓；空=不导出）")
    ap.add_argument("--writer", default=None, help="写入方标识（默认 None）")
    args = ap.parse_args(argv)
    run(args.qlab, args.out, skip_blackbox=args.skip_blackbox,
        summary_out=args.summary_out, detail_out=args.detail_out or None,
        writer=args.writer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
