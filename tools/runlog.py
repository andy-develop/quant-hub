#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/runlog.py —— §3.1 CI runlog 台账写入。

每个域一份：`state/<domain>/runlog/YYYY-MM-DD.json`（北京时区当天）。
策略链路(strategy-pm/am)、数据链路(data-retention)、发布链路(build-publish)
各自在跑完后调用本模块落盘，供 repo-health 周巡检检查"最近 5 个交易日链路是否健康"。

stages 约定：`{stage_name: 状态/描述}`，状态含 fail/failed/error/exception
会被 repo-health 判为告警；`data_date` 为策略数据对应交易日。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

TZ_CST = _dt.timezone(_dt.timedelta(hours=8))


def write_runlog(state_root: str | Path, domain: str, *,
                 stages: dict | None = None,
                 data_date: str | None = None,
                 trigger: str = "schedule",
                 run_date: str | None = None,
                 extra: dict | None = None) -> Path:
    """写一份 runlog 并返回路径。同日同名覆盖（幂等，可补跑）。

    state_root: state/ 目录（写入 state_root/<domain>/runlog/YYYY-MM-DD.json）
    stages: {stage_name: 状态/描述}；含 fail/failed/error/exception 会被 repo-health 告警。
    """
    state_root = Path(state_root)
    today = run_date or _dt.datetime.now(TZ_CST).strftime("%Y-%m-%d")
    rec = {
        "domain": domain,
        "run_date": today,
        "trigger": trigger,
        "stages": stages or {},
        "updated_at": _dt.datetime.now(TZ_CST).isoformat(timespec="seconds"),
    }
    if data_date:
        rec["data_date"] = data_date
    if extra:
        rec.update(extra)

    out_dir = state_root / domain / "runlog"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{today}.json"
    tmp = out_dir / f"{today}.json.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def read_runlogs(state_root: str | Path, domain: str, n: int = 5) -> list[dict]:
    """读最近 n 份 runlog（按文件名排序，容错跳过坏 JSON）。"""
    d = Path(state_root) / domain / "runlog"
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json"))[-n:]:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return out


def recent_days(state_root: str | Path, domain: str, n: int = 5) -> int:
    """最近有 runlog 落盘的天数（repo-health 判链路活性用）。"""
    days = set()
    for rec in read_runlogs(state_root, domain, n * 2):
        days.add(rec.get("run_date"))
    return len(days)


if __name__ == "__main__":
    import sys

    state = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state")
    dom = sys.argv[2] if len(sys.argv) > 2 else "demo"
    stages = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    print(write_runlog(state, dom, stages=stages))
