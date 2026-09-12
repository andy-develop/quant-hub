#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影子并行逐位比对（方案 §7.3 —— 整个迁移最重要的一件事）。

对比"新链路自产 payload"与"老链路转发 payload"，任何一位不同就 BLOCKED、不带病晋级。
差异根因只会来自（按历史频率）：复权口径(§0.3)、volume 单位(股 vs 万元)、代码格式统一顺序、
交易日边界、缺失值处理、除权 fixup 覆盖范围。

比对三件事：
  1. data_date 是否一致
  2. 归一化 payload 指纹（剔除 generated_at 等每次都变的字段）是否一致
  3. KPI 数值最大绝对偏差是否 <= 容差

输出每域 verdict ∈ {PROMOTE-READY, BLOCKED, NOT-STARTED, MISSING}，可写进 runlog（§3.1）。

用法：
    python -m tools.shadow_diff --shadow state/payload --legacy state/payload_legacy \
        --out state/shadow_diff.json [--tol 1e-9]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

# 每次都变、不参与指纹的字段
VOLATILE = {"generated_at", "built_at", "timestamp", "run_id", "workflow_run_id"}
# 视为 KPI 的数值字段名（小写子串匹配）
KPI_KEYS = ("ret", "return", "sharpe", "mdd", "drawdown", "trade", "win", "cagr",
            "vol", "alpha", "beta", "ir", "calmar", "exposure", "nav")


def _load_dir(d: str) -> dict:
    """读 state/payload/*.json 统一信封 -> {(domain,variant): envelope}。"""
    out = {}
    if not d or not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        dom, var = obj.get("domain"), obj.get("variant")
        if dom and var:
            out[(dom, var)] = obj
    return out


def _strip_volatile(o):
    """递归剔除 VOLATILE 字段，得到可逐位比对的结构。"""
    if isinstance(o, dict):
        return {k: _strip_volatile(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [_strip_volatile(v) for v in o]
    return o


def _fingerprint(payload) -> str:
    canon = json.dumps(_strip_volatile(payload), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _collect_kpis(o, path="") -> dict:
    """递归收集 KPI 数值叶子：{扁平路径: float}。只收键名命中 KPI_KEYS 的数值。"""
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if any(t in str(k).lower() for t in KPI_KEYS):
                    out[p] = float(v)
            else:
                out.update(_collect_kpis(v, p))
    elif isinstance(o, list):
        # 列表只在元素是 dict 时下钻（如 trades/holdings 明细，逐条 KPI 不展开，避免噪声）
        pass
    return out


def compare_one(shadow_env: dict, legacy_env: dict, tol: float) -> dict:
    sp, lp = shadow_env.get("payload"), legacy_env.get("payload")
    sd, ld = shadow_env.get("data_date"), legacy_env.get("data_date")
    sfp, lfp = _fingerprint(sp), _fingerprint(lp)
    sk, lk = _collect_kpis(sp), _collect_kpis(lp)

    kpi_deltas = {}
    for k in sorted(set(sk) | set(lk)):
        a, b = sk.get(k), lk.get(k)
        if a is None or b is None:
            kpi_deltas[k] = None       # 一边缺该 KPI
        else:
            kpi_deltas[k] = abs(a - b)
    numeric = [v for v in kpi_deltas.values() if v is not None]
    max_delta = max(numeric) if numeric else 0.0
    missing_kpi = [k for k, v in kpi_deltas.items() if v is None]

    data_date_equal = (sd == ld)
    sha_equal = (sfp == lfp)
    kpi_equal = (max_delta <= tol) and not missing_kpi

    if data_date_equal and kpi_equal and sha_equal:
        verdict = "PROMOTE-READY"
        hint = "逐位一致（data_date + KPI + 归一化指纹）"
    else:
        verdict = "BLOCKED"
        hints = []
        if not data_date_equal:
            hints.append(f"data_date 不一致 shadow={sd} legacy={ld}（疑似交易日边界差一天）")
        if missing_kpi:
            hints.append(f"KPI 字段缺失/多出: {missing_kpi[:5]}")
        if max_delta > tol:
            worst = max((k for k in kpi_deltas if kpi_deltas[k] is not None),
                        key=lambda k: kpi_deltas[k], default=None)
            hints.append(f"KPI 最大偏差 {max_delta:.3e} > {tol:.0e}（{worst}）"
                         f"—— 查复权口径/volume单位/代码格式顺序/缺失值")
        if not sha_equal and kpi_equal and data_date_equal:
            hints.append("KPI 与 data_date 一致但归一化指纹不同（非 KPI 字段有差异，需人工确认）")
        hint = "; ".join(hints)

    return {
        "data_date_equal": data_date_equal,
        "kpi_equal": kpi_equal,
        "sha_equal": sha_equal,
        "max_kpi_delta": max_delta,
        "kpi_deltas": {k: v for k, v in kpi_deltas.items() if v},
        "payload_sha": {"shadow": sfp[:16], "legacy": lfp[:16]},
        "data_date": {"shadow": sd, "legacy": ld},
        "verdict": verdict,
        "hint": hint,
    }


def diff(shadow_dir: str, legacy_dir: str, *, tol: float = 1e-9,
         domains=("selected", "etf", "shortterm")) -> dict:
    sh, lg = _load_dir(shadow_dir), _load_dir(legacy_dir)
    out = {}
    keys = sorted(set(sh) | set(lg))
    for k in keys:
        dom, var = k
        if k in sh and k in lg:
            out[f"{dom}/{var}"] = compare_one(sh[k], lg[k], tol)
        elif k in sh:
            out[f"{dom}/{var}"] = {"verdict": "MISSING", "hint": "legacy 缺该 payload（无法比对）"}
        else:
            out[f"{dom}/{var}"] = {"verdict": "NOT-STARTED", "hint": "shadow 尚未自产该 payload"}
    # 汇总每域最差 verdict（晋级以域为单位）
    by_domain = {}
    for k, v in out.items():
        dom = k.split("/")[0]
        rank = {"PROMOTE-READY": 0, "NOT-STARTED": 1, "MISSING": 2, "BLOCKED": 3}
        cur = by_domain.get(dom)
        if cur is None or rank.get(v["verdict"], 9) > rank.get(cur["verdict"], 9):
            by_domain[dom] = v
    return {"tolerance": tol, "by_variant": out, "by_domain": by_domain}


# ---------------------------------------------------------------------------
# Phase-4 自动晋级台账：连续 N 个交易日 PROMOTE-READY 才把某域 shadow→primary
# ---------------------------------------------------------------------------
def update_promotion(ledger_path: str, by_domain: dict, trading_day: str,
                     threshold: int = 3) -> dict:
    """推进每域的晋级计数（方案 §7.2 Phase-4：连续 3 个交易日逐位一致才晋级）。

    - PROMOTE-READY：consecutive +1（同一 trading_day 重复跑不重复计数，幂等）
    - 其它 verdict（BLOCKED/NOT-STARTED/MISSING）：consecutive 归零（不带病晋级）
    - consecutive >= threshold：promoted=true（上层据此把 payload_source 由 shadow 转 primary）
    台账入 git，可审计、可回滚。
    """
    ledger = {}
    if ledger_path and os.path.exists(ledger_path):
        try:
            with open(ledger_path, encoding="utf-8") as f:
                ledger = json.load(f)
        except Exception:  # noqa: BLE001
            ledger = {}

    for dom, v in by_domain.items():
        st = ledger.get(dom, {"consecutive": 0, "promoted": False,
                              "last_day": None, "history": []})
        verdict = v.get("verdict")
        if st.get("last_day") == trading_day:
            # 同一交易日已记过：只更新 verdict 展示，不重复 +/- 计数
            st["last_verdict"] = verdict
        else:
            if verdict == "PROMOTE-READY":
                st["consecutive"] = int(st.get("consecutive", 0)) + 1
            else:
                st["consecutive"] = 0
            st["promoted"] = st["consecutive"] >= threshold
            st["last_day"] = trading_day
            st["last_verdict"] = verdict
            st.setdefault("history", []).append(
                {"day": trading_day, "verdict": verdict, "consecutive": st["consecutive"]})
            st["history"] = st["history"][-30:]      # 只留最近 30 条
        ledger[dom] = st

    if ledger_path:
        os.makedirs(os.path.dirname(os.path.abspath(ledger_path)) or ".", exist_ok=True)
        tmp = ledger_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ledger_path)
    return ledger


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="影子并行逐位比对（§7.3）")
    ap.add_argument("--shadow", required=True, help="新链路自产 payload 目录")
    ap.add_argument("--legacy", required=True, help="老链路转发 payload 目录")
    ap.add_argument("--out", default=None, help="结果写 runlog（JSON）")
    ap.add_argument("--tol", type=float, default=1e-9)
    ap.add_argument("--strict", action="store_true", help="任一域 BLOCKED 则退出码非 0")
    ap.add_argument("--ledger", default=None, help="晋级台账 JSON（连续 N 交易日 PROMOTE-READY 自动晋级）")
    ap.add_argument("--trading-day", default=None, help="本次比对对应的交易日 YYYY-MM-DD（台账计数用）")
    ap.add_argument("--threshold", type=int, default=3, help="晋级所需连续交易日数（默认 3）")
    args = ap.parse_args(argv)

    res = diff(args.shadow, args.legacy, tol=args.tol)
    print(json.dumps(res["by_domain"], ensure_ascii=False, indent=2))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"[i] 写入 {args.out}")
    if args.ledger and args.trading_day:
        led = update_promotion(args.ledger, res["by_domain"], args.trading_day, args.threshold)
        for dom, st in led.items():
            flag = "✓ PROMOTED" if st.get("promoted") else f"{st.get('consecutive',0)}/{args.threshold}"
            print(f"[promotion] {dom}: {flag} (last={st.get('last_verdict')})")
    if args.strict:
        blocked = [d for d, v in res["by_domain"].items() if v["verdict"] == "BLOCKED"]
        if blocked:
            print(f"[✗] BLOCKED 域: {blocked} —— 不带病晋级")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
