#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Day-1 转发适配器（方案 §7.1）：从三个老仓的**现有产物**抽出 payload，规范化为统一信封。

零改动老仓、零联网：老仓 CI 照旧每天跑、产物 commit 在老仓，本脚本只读取并转发。
这是合并页"当天就有真实数据"的最短路径，后续 Phase-4 逐域换成自产（读 quant-hub-data）。

各域来源（实测）：
  etf      —— red-dividend-strategy/index.html 里 <script id="PAYLOAD">（308KB，含 snapshot/backtest/hs300/sector）✓ 真实
  selected —— stock-factor-engine/data/{stocks,factors}.json（stocks 真实 5180；factors 当前空 → §9.1 如实告警）✓
  shortterm—— quant-lab **不 commit** 计算产物（report/ 与 data/meta/* 均 gitignore），无法转发；
              需策略链路实际运行 signals+engine+build_report 才有真实数据 → 本脚本对其返回"待运行"告警信封，
              合并页该域走 carry-forward / 空态，绝不塞假数据。

用法：
    python -m tools.forward_payloads --src src --out state/payload
    #   src 下含 red-dividend-strategy/ stock-factor-engine/ quant-lab/（或 domains/{etf,selected,shortterm}）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.payload.adapters import (normalize_etf, normalize_stock,  # noqa: E402
                                     normalize_quant_lab, write_envelopes, DomainPayload)

_PAYLOAD_RE = re.compile(r'<script id="PAYLOAD" type="application/json">(.*?)</script>', re.S)

# 老仓目录名 / 合并后 domains 名 都可
_ALIASES = {
    "etf": ("red-dividend-strategy", "etf"),
    "selected": ("stock-factor-engine", "selected"),
    "shortterm": ("quant-lab", "shortterm"),
}


def _base(src_root: str, domain: str):
    for cand in _ALIASES[domain]:
        p = os.path.join(src_root, cand)
        if os.path.isdir(p):
            return p
    return None


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def forward_etf(src_root: str):
    base = _base(src_root, "etf")
    if not base:
        return []
    html_path = os.path.join(base, "index.html")
    if not os.path.exists(html_path):
        return []                              # domains/etf 不含 index.html（产物）→ 需老仓检出
    m = _PAYLOAD_RE.search(_read(html_path))
    if not m:
        return []
    try:
        payload = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return []
    data_date = None
    snap = payload.get("snapshot") or {}
    for k in ("data_date", "date", "asof", "updated"):
        if isinstance(snap, dict) and snap.get(k):
            data_date = str(snap[k])[:10]
            break
    return normalize_etf(payload=payload, data_date=data_date)


def forward_selected(src_root: str):
    base = _base(src_root, "selected")
    if not base:
        return []
    d = os.path.join(base, "data")
    sp, fp = os.path.join(d, "stocks.json"), os.path.join(d, "factors.json")
    if not os.path.exists(sp):
        return []
    try:
        stocks = json.load(open(sp, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        stocks = []
    try:
        factors = json.load(open(fp, encoding="utf-8")) if os.path.exists(fp) else {}
    except Exception:  # noqa: BLE001
        factors = {}
    return normalize_stock(stocks=stocks, factors=factors)


def forward_shortterm(src_root: str):
    """quant-lab 不 commit 计算产物：有 report 就转发，没有就返回"待运行"告警信封（不塞假数据）。"""
    base = _base(src_root, "shortterm")
    if not base:
        return []
    # 若检出的 quant-lab 里有已构建报告（本地全量跑过 / 将来 commit），尝试抽 MODES
    for rel in ("report/index.html", "index.html"):
        p = os.path.join(base, rel)
        if os.path.exists(p):
            html = _read(p)
            mm = re.search(r'const MODES\s*=\s*(\{.*?\});', html, re.S)
            mb = re.search(r'const MODES_BB\s*=\s*(\{.*?\});', html, re.S)
            if mm:
                try:
                    modes = json.loads(mm.group(1))
                    modes_bb = json.loads(mb.group(1)) if mb else None
                    return normalize_quant_lab(modes=modes, modes_bb=modes_bb)
                except Exception:  # noqa: BLE001
                    pass
    # 没有可转发的真实产物 -> 不写信封（返回空），让合并页该域走 carry-forward / 空态。
    # ★ 不写空占位信封：否则会覆盖将来 Phase-2 自产/上次成功的 shortterm payload（carry-forward 不安全）。
    print("[i] 短线域无可转发产物（quant-lab 不 commit report/data-meta）→ 空态，待 Phase-2 策略链路自产")
    return []


def forward_all(src_root: str) -> list:
    out = []
    out += forward_etf(src_root)
    out += forward_selected(src_root)
    out += forward_shortterm(src_root)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Day-1 转发适配器（§7.1）")
    ap.add_argument("--src", required=True, help="三老仓检出根（或含 domains/ 的仓根）")
    ap.add_argument("--out", default="state/payload")
    args = ap.parse_args(argv)
    payloads = forward_all(args.src)
    paths = write_envelopes(payloads, args.out)
    print(f"[✓] 转发 {len(paths)} 个 payload 信封 -> {args.out}")
    for p in payloads:
        w = f" ⚠{len(p.warnings)}" if getattr(p, "warnings", None) else ""
        print(f"   - {p.domain}.{p.variant}{w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
