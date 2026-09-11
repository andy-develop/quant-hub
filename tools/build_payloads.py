#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三域 payload 自产编排（方案 §3 state/<d>/payload + §4.5 carry-forward）。

把"已就位的数据/产物"规范化为统一信封写到 `state/payload/`，供 web.build 注入合并页。
缺数据的域**不产空信封**，而是留空由 build-publish 走 carry-forward（读上次成功版本 + 状态条标红），
绝不因为某域没数据就让页面塞假数据（§4.5）。

当前可自产：
  selected —— domains/selected/data/{stocks,factors}.json（stocks 真实 5180 只；
              factors 为空时按 §9.1 如实告警，不伪造）
待数据仓首灌后接入（Phase-4，逐域晋级）：
  etf / shortterm —— 由各自引擎读 data/ 产出 KPI/净值/信号后写 state/<d>/，本脚本再适配

用法：
    python -m tools.build_payloads --repo-root . --out state/payload
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.payload.adapters import normalize_stock, write_envelopes  # noqa: E402


def _read_json(path, default):
    if not os.path.exists(path):
        return default, False
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), True
    except Exception:  # noqa: BLE001
        return default, False


def build_selected(repo_root: str):
    """个性化选股域：stocks.json（[code,name] 二元组列表）+ factors.json。

    normalize_stock 自带告警（stocks 空 / factors 空 / 格式异常），无需在此重复；
    factors 为空时它会如实标注 —— 正是 §9.1 要的"不伪造、如实披露"。
    """
    d = os.path.join(repo_root, "domains", "selected", "data")
    stocks, ok_s = _read_json(os.path.join(d, "stocks.json"), [])
    factors, _ok_f = _read_json(os.path.join(d, "factors.json"), {})
    if not ok_s:
        return []                              # 连股票池都没有 -> 不产，交给 carry-forward
    return normalize_stock(stocks=stocks, factors=factors)


def build(repo_root: str) -> list:
    payloads = []
    payloads += build_selected(repo_root)
    # etf / shortterm：待各自引擎产出 state/<d>/ 后在此适配（Phase-4）
    return payloads


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="三域 payload 自产编排")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--out", default="state/payload")
    args = ap.parse_args(argv)

    payloads = build(args.repo_root)
    paths = write_envelopes(payloads, args.out)
    print(f"[✓] 写出 {len(paths)} 个 payload 信封 -> {args.out}")
    for p in paths:
        print("   -", os.path.relpath(p, args.repo_root))
    if not paths:
        print("[i] 无可自产 payload（数据未就位）—— build-publish 将走 carry-forward")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
