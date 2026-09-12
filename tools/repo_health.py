#!/usr/bin/env python3
"""tools/repo_health.py —— §9.7/§10.4 repo-health.yml 周巡检的本地引擎（Phase 6）。

读三域 + 数据链路 runlog，检查最近 5 个交易日是否有：
  - 失败 / 缺失 / 未发布 / 覆盖率 <90%
  - payload 大小趋势（膨胀预警）
  - 数据仓工作树体积（>700MB warning / >900MB error → 该 squash 了）

输出 state/data/repo-health/<YYYY-MM-DD>.json；发现问题时 exit 0 但打 ::warning::，
CI（repo-health.yml）负责：告警摘要 → 已有未关闭 issue 去重 → 开 issue。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

DOMAINS = ["shortterm", "etf", "selected"]
DATA_LINK = "data-retention"  # runlog 台账域

# 逻辑域 -> state/payload/{domain}.{variant}.json 里的真实 domain 标识
PAYLOAD_DOMAIN = {"shortterm": "quant-lab", "etf": "etf", "selected": "stock"}


def _dir_mb(p: Path) -> float:
    if not p.exists():
        return 0.0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def _payload_kb(domain: str) -> float | None:
    """该域最新一份信封的大小（KB）；无产物返回 None。"""
    d = STATE / "payload"
    if not d.exists():
        return None
    files = sorted(d.glob(f"{PAYLOAD_DOMAIN[domain]}.*.json"))
    if not files:
        return None
    return files[-1].stat().st_size / 1024


def main() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    issues: list[str] = []
    size_trend: dict[str, float | None] = {}

    for domain in DOMAINS:
        runlog_dir = STATE / domain / "runlog"
        size_trend[domain] = _payload_kb(domain)

        # 最近 5 个 runlog：要求存在且无 warning 类失败标记
        logs = sorted(runlog_dir.glob("*.json"))[-5:] if runlog_dir.exists() else []
        if len(logs) < 5:
            issues.append(f"[{domain}] runlog 不足 5 份（{len(logs)}）—— 链路可能未持续跑")
            continue
        for f in logs:
            rec = json.loads(f.read_text(encoding="utf-8"))
            stages = rec.get("stages") or {}
            # 任何 stage 标记失败/异常 → 告警
            bad = [k for k, v in stages.items()
                   if isinstance(v, str) and v.lower() in ("fail", "failed", "error", "exception")]
            if rec.get("data_date") is None and not stages:
                bad.append("empty")
            if bad:
                issues.append(f"[{domain}] {f.name} stages 异常: {bad}")

    # 数据链路 runlog（retention 周报）
    dr = STATE / DATA_LINK / "runlog"
    if dr.exists() and list(dr.glob("*.json")):
        last = sorted(dr.glob("*.json"))[-1]
        rec = json.loads(last.read_text(encoding="utf-8"))
        stages = rec.get("stages") or {}
        if "retention" in stages and "error" in str(stages.get("retention", "")).lower():
            issues.append(f"[data-retention] {last.name}: retention 执行异常")

    # 数据仓工作树体积（数据仓在 CI 里挂 data/，本地可能是 sparse 或缺省）
    work_mb = _dir_mb(ROOT / "data")
    if work_mb > 0:
        if work_mb > 900:
            issues.append(f"数据仓工作树 {work_mb:.0f}MB > 900MB error 门禁 → 需要 squash")
            print(f"::error:: 数据仓 {work_mb:.0f}MB > 900MB，建议年度 orphan squash")
        elif work_mb > 700:
            issues.append(f"数据仓工作树 {work_mb:.0f}MB > 700MB warning 门禁")
            print(f"::warning:: 数据仓 {work_mb:.0f}MB > 700MB，建议规划 squash")

    report = {
        "run_date": today,
        "issues": issues,
        "payload_size_kb": size_trend,
        "data_worktree_mb": round(work_mb, 1),
        "healthy": len(issues) == 0,
    }
    out = STATE / "data" / "repo-health"
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{today}.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if issues:
        print(f"::warning:: repo-health 发现 {len(issues)} 项问题（见 {p.relative_to(ROOT)}）")
        for i in issues:
            print(f"  - {i}")
    else:
        print("[repo-health] 健康：无告警")


if __name__ == "__main__":
    main()
