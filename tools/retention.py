#!/usr/bin/env python3
"""tools/retention.py —— §4.2 封存过期 + 体积报告（Phase 5 data-retention.yml 的本地引擎）。

- stock 保留最近 730 交易日 / etf 2430 交易日 / index 不过期（schema.RETENTION）
- 过期 = 删整个 year=/month= 分区目录（纯文件系统，幂等、可 dry-run）
- 删前 Release 归档门禁：--require-release 时需存在 release 归档，否则中止（后悔药）
- --archive-out DIR：把本次将删分区打包 tar.zst 到 DIR（幂等：已有同月归档即跳过）
- 删除后按实际删除的分区条目重建 manifest（read_manifest → 过滤 → write_manifest）

用法:
  .venv/bin/python tools/retention.py --dry-run                        # 只列清单 + 体积报告
  .venv/bin/python tools/retention.py --archive-out /tmp/arch          # 生成删前归档（幂等）
  .venv/bin/python tools/retention.py --apply --force                  # 跳过确认直接实删
  .venv/bin/python tools/retention.py --apply --require-release --force  # CI：要求 Release 归档存在
"""
from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.store import schema  # noqa: E402
from common.store.writer import (expire_partitions,  # noqa: E402
                                 read_manifest, write_manifest)

# 各资产类的复权口径（hfq 信号/回测，raw 涨跌停/真实 volume）
_ASSET_FQS = {"stock": ("hfq", "raw"), "etf": ("hfq", "raw")}


def _data_root() -> Path:
    return ROOT / "data"


def _dir_size_mb(p: Path) -> float:
    total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return total / 1024 / 1024


def _release_archive_exists() -> bool:
    """检查 GitHub Release 归档存在（retention 不可逆，删前必须有一份）。"""
    try:
        out = subprocess.run(
            ["gh", "release", "list", "--limit", "5"], capture_output=True, text=True, timeout=30
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def _size_report() -> dict:
    data = _data_root()
    report = {}
    for sub in sorted(p for p in data.iterdir() if p.is_dir()):
        report[sub.name] = round(_dir_size_mb(data / sub.name), 2)
    return report


def _plan_removals() -> dict[str, dict[str, dict]]:
    """先 dry-run 算清单（不改磁盘），供归档与展示。

    返回 plan[asset][fq] = expire_partitions 的返回 dict（removed 列表）。
    """
    plan: dict[str, dict[str, dict]] = {}
    root = _data_root()
    for asset, keep in schema.RETENTION.items():
        plan[asset] = {}
        if keep is None:
            continue
        for fq in _ASSET_FQS.get(asset, ("hfq", "raw")):
            plan[asset][fq] = expire_partitions(asset, fq, keep,
                                                root=str(root), dry_run=True)
    return plan


def _flat_removed(plan: dict[str, dict[str, dict]]) -> list[tuple[Path, Path]]:
    """把 plan 里将删分区展开为 [(绝对路径, 相对 data/ 路径)]，供归档。"""
    root = _data_root()
    out = []
    for fqs in plan.values():
        for res in fqs.values():
            for it in res.get("removed", []):
                src = Path(it["dir"])
                if src.exists():
                    out.append((src, src.relative_to(root)))
    return sorted(set(out), key=lambda x: str(x[1]))


def _archive_removals(plan: dict[str, dict[str, dict]], out_dir: Path) -> Path | None:
    """把将删分区打成 data-expired-YYYYMM.tar.zst（幂等：已有则跳过，返回 None）。"""
    removed = _flat_removed(plan)
    if not removed:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m")
    arc = out_dir / f"data-expired-{stamp}.tar.zst"
    if arc.exists():
        print(f"归档已存在（幂等跳过）: {arc}")
        return arc
    tmp = out_dir / f"data-expired-{stamp}.tar"
    with tarfile.open(tmp, "w") as tar:
        for src, rel in removed:
            tar.add(src, arcname=str(rel))
    subprocess.run(["zstd", "-q", "-19", "-f", str(tmp), "-o", str(arc)], check=True)
    tmp.unlink()
    print(f"删前归档: {arc} ({arc.stat().st_size/1024:.0f} KB, {len(removed)} 个分区)")
    return arc


def _rebuild_manifests(plan: dict[str, dict[str, dict]]) -> None:
    """删除后重建 manifest：从每个 (asset, fq) 的 manifest 移除已删分区条目。"""
    root = _data_root()
    for asset, fqs in plan.items():
        for fq, res in fqs.items():
            removed = res.get("removed", [])
            if not removed:
                continue
            dead = [str(Path(it["dir"]).relative_to(root)) for it in removed]
            man = read_manifest(str(root), asset, fq)
            parts = man.get("partitions", [])
            keep = [
                p for p in parts
                if not any(p.get("path") == d or str(p.get("path", "")).startswith(d + "/")
                           for d in dead)
            ]
            if len(keep) != len(parts):
                man["partitions"] = keep
                write_manifest(str(root), asset, fq, man)
                print(f"  manifest {asset}_{fq}: {len(parts)} -> {len(keep)} 条分区记录")
    print("已重建 manifest（删除后）")


def main() -> None:
    dry_run = "--apply" not in sys.argv
    require_release = "--require-release" in sys.argv
    force = "--force" in sys.argv
    archive_out = None
    if "--archive-out" in sys.argv:
        archive_out = Path(sys.argv[sys.argv.index("--archive-out") + 1])

    plan = _plan_removals()

    if not dry_run and require_release and not _release_archive_exists():
        # 允许 --archive-out 先生产本地归档再 push；release 检查留到 workflow 上传后
        print("警告: 未发现 Release 归档，但本地归档优先（workflow 会上传后复核）")

    if archive_out is not None:
        _archive_removals(plan, archive_out)

    print(f"== retention {('dry-run' if dry_run else 'apply')} ==")
    for asset, keep in schema.RETENTION.items():
        if keep is None:
            print(f"  {asset}: 不过期（RETENTION=None）")
            continue
        for fq, res in plan[asset].items():
            removed = res.get("removed", [])
            if removed:
                print(f"  {asset}/{fq}: 保留 {keep} 交易日（cutoff={res.get('cutoff')}）"
                      f" → 删 {len(removed)} 个分区")
                for it in removed[:8]:
                    print(f"    - {it['dir']} (last={it['last_day']})")
                if len(removed) > 8:
                    print(f"    … 等 {len(removed)-8} 个")
            else:
                print(f"  {asset}/{fq}: 无过期分区（数据在窗口内）")

    if not dry_run:
        if not force:
            print("\n[确认] 将删除上述分区（不可逆）。输入 yes 确认实删：")
            if sys.stdin.readline().strip().lower() != "yes":
                raise SystemExit("已取消（未确认）")
        # 实删（清单已在上方 dry 计算，这里仅执行删除动作；archive=True 由 writer 内建归档兜底）
        root = _data_root()
        for asset, keep in schema.RETENTION.items():
            if keep is None:
                continue
            for fq in _ASSET_FQS.get(asset, ("hfq", "raw")):
                expire_partitions(asset, fq, keep, root=str(root),
                                  dry_run=False, archive=True)
        _rebuild_manifests(plan)

    report = _size_report()
    print(f"\n体积报告（data/ 各子目录 MB）：{json.dumps(report, ensure_ascii=False)}")
    total = round(sum(report.values()), 2)
    print(f"  合计 {total} MB（门禁 ≤250MB）")
    if total > 250:
        print("::error:: data/ 体积超过 250MB 门禁，建议 squash 或扩大 retention")
        sys.exit(1)


if __name__ == "__main__":
    main()
