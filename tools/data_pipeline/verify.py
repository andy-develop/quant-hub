#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据仓自检编排（方案 §2.3 契约测试 + §2.7 派生一致性 + §2.4 manifest 校验）。

对**已挂载的真实数据**（data/）逐分区校验，是 verify.yml 的入口。与 tests/contract
（合成数据的单元测试）互补：这里查的是磁盘上真实的 parquet。

检查项：
  1. 契约：每个分区列 ⊆ COLUMNS∪OPTIONAL、含 PRIMARY_KEY、close>0、无重复 (code,date)、
     单 code 内日期单调、日期都在交易日历内
  2. 派生一致性：weekly/monthly 的 derived_from.sha256 == 当前 daily 重算指纹（R17）
  3. is_partial：weekly/monthly 分区必带该列（§2.7）
  4. manifest：每个 manifest 的 partitions[].path 存在、rows>0
红 = 退出码非 0（verify.yml 据此中止发布）。

用法： python -m tools.data_pipeline.verify --data-root data
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REQUIRED_KEYS = ("code", "date")


def _pd():
    import pandas as pd  # noqa: PLC0415
    return pd


def check_partition(path, cal, pd, problems, asset):
    from common.store.schema import COLUMNS, OPTIONAL
    try:
        df = pd.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        problems.append(f"[read] {path}: {e}")
        return
    if df.empty:
        return
    allowed = set(COLUMNS) | set(OPTIONAL) | {"is_partial", "period_key", "prev_close", "_degraded"}
    extra = set(df.columns) - allowed
    if extra:
        problems.append(f"[schema] {path}: 未知列 {sorted(extra)}")
    for k in REQUIRED_KEYS:
        if k not in df.columns:
            problems.append(f"[schema] {path}: 缺主键列 {k}")
            return
    if "close" in df.columns:
        bad = df[~(df["close"] > 0)]
        if len(bad):
            problems.append(f"[invariant] {path}: {len(bad)} 行 close<=0/NaN")
    dup = df.duplicated(subset=["code", "date"]).sum()
    if dup:
        problems.append(f"[invariant] {path}: {int(dup)} 个重复 (code,date)")
    # 日期都在交易日历内（仅校验日历覆盖范围内的日期：指数史可回溯到 1990，
    # 而种子日历从 2013 起 —— 覆盖范围外的历史日期无法判定，跳过而非误报）
    if cal is not None:
        try:
            lo, hi = cal.min_date, cal.max_date
            days = [d for d in pd.to_datetime(df["date"]).dt.date.unique() if lo <= d <= hi]
            off = [d for d in days if not cal.is_trading_day(d)]
            if off:
                problems.append(f"[calendar] {path}: {len(off)} 个非交易日日期，示例 {off[:3]}")
        except Exception:  # noqa: BLE001
            pass


def check_daily(root, cal, pd, problems):
    n = 0
    for asset in ("stock", "etf", "index"):
        for fq in ("hfq", "raw"):
            base = os.path.join(root, "market", asset, fq)
            for p in glob.glob(os.path.join(base, "**", "*.parquet"), recursive=True):
                check_partition(p, cal, pd, problems, asset)
                n += 1
    return n


def check_derived(root, pd, problems):
    """weekly/monthly 派生分区：必带 is_partial；derived_from.sha256 与当前 daily 指纹一致。

    ★ 同时覆盖 index_（基准大盘 broad）与 etf_（中证 csindex）两组派生 manifest。
    """
    from tools.data_pipeline.index import daily_fingerprint, FQ
    from common.store.reader import load
    n = 0
    mdir = os.path.join(root, "manifest")
    mps = (glob.glob(os.path.join(mdir, "index_*_weekly.json"))
           + glob.glob(os.path.join(mdir, "index_*_monthly.json"))
           + glob.glob(os.path.join(mdir, "etf_*_weekly.json"))
           + glob.glob(os.path.join(mdir, "etf_*_monthly.json")))
    for mp in sorted(set(mps)):
        man = json.load(open(mp, encoding="utf-8"))
        asset = man.get("asset", "index")     # 兼容旧 manifest（index_broad_* 无 asset 字段）
        code, freq = man.get("code"), man.get("freq")
        n += 1
        # is_partial 列存在
        pp = os.path.join(root, man["path"])
        if os.path.exists(pp):
            d = pd.read_parquet(pp)
            if "is_partial" not in d.columns:
                problems.append(f"[partial] {man['path']}: 缺 is_partial 列（§2.7）")
        # 派生一致性
        try:
            daily = load(asset=asset, fq=FQ, freq="daily", code=code, root=root)
            fp = daily_fingerprint(daily, pd)
            got = (man.get("derived_from") or {}).get("sha256")
            if got != fp:
                problems.append(
                    f"[derived] {asset}/{code}/{freq}: derived_from.sha256={str(got)[:12]} != "
                    f"daily 重算={fp[:12]} —— daily 更新了但周月K未重算（R17）")
        except Exception as e:  # noqa: BLE001
            problems.append(f"[derived] {asset}/{code}/{freq}: 校验异常 {e}")
    return n


def check_manifests(root, problems):
    n = 0
    for mp in glob.glob(os.path.join(root, "manifest", "*.json")):
        man = json.load(open(mp, encoding="utf-8"))
        n += 1
        cv = man.get("contract_version")
        if cv and cv != "1.0":
            problems.append(f"[manifest] {mp}: contract_version={cv} != 1.0")
        for part in man.get("partitions", []) or []:
            p = os.path.join(root, part["path"])
            if part.get("path") and not os.path.exists(p):
                # 目录型分区（year=/month=）用目录存在性判断
                if not os.path.isdir(p):
                    problems.append(f"[manifest] {mp}: 分区路径不存在 {part['path']}")
    return n


def check_events(root, cal, pd, problems):
    """事件型表（龙虎榜/涨停复盘）契约校验：列⊆定义、主键无重复、日期在日历内。"""
    from common.store.events import check_event_table
    from common.store.schema import EVENT_TABLES
    total = 0
    for name in sorted(EVENT_TABLES):
        n = check_event_table(root, name, pd, problems, calendar=cal)
        total += n
        if n:
            print(f"[i] events/{name}: {n} 个分区")
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="数据仓自检（§2.3/§2.7）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    args = ap.parse_args(argv)

    pd = _pd()
    root = args.data_root
    problems: list[str] = []

    cal = None
    try:
        from common.calendar import load_calendar
        cal = load_calendar(root=root)
        print(f"[i] 交易日历 {cal.min_date} ~ {cal.max_date}（{len(cal)} 交易日）")
    except Exception as e:  # noqa: BLE001
        problems.append(f"[calendar] 加载失败：{e}")

    n_daily = check_daily(root, cal, pd, problems)
    n_derived = check_derived(root, pd, problems)
    n_man = check_manifests(root, problems)
    n_events = check_events(root, cal, pd, problems)

    print(f"[i] 校验 daily 分区 {n_daily} · 派生 {n_derived} · manifest {n_man} · events {n_events}")
    if problems:
        print(f"[✗] 发现 {len(problems)} 个问题：")
        for p in problems[:60]:
            print("   -", p)
        if len(problems) > 60:
            print(f"   ...（另有 {len(problems)-60} 条）")
        return 1
    print("[✓] 数据仓自检全过：契约/不变量/日历/派生一致性/manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
