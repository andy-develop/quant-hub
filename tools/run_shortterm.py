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


def run(qlab: str, out: str, *, skip_blackbox: bool = False, logger=print) -> dict:
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
    return {"payloads": len(paths), "data_date": data_date,
            "has_blackbox": bool(modes_bb)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="短线域自产（跑 quant-lab 计算链）")
    ap.add_argument("--qlab", required=True, help="quant-lab 检出根（含 data/kline + scripts）")
    ap.add_argument("--out", default="state/payload")
    ap.add_argument("--skip-blackbox", action="store_true", help="跳过 LightGBM 黑盒（省时/无 lightgbm 时）")
    args = ap.parse_args(argv)
    run(args.qlab, args.out, skip_blackbox=args.skip_blackbox)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
