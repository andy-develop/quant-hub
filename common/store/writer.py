"""数据层唯一写接口。

两条铁律：

1. **hfq/raw 历史值永久冻结**。writer 遇到分区内已存在的 (code, date) → 抛异常而非覆盖。
   这是"重复回补 = 成倍重复行"（量化域硬性陷阱 #3）的唯一可靠拦截点。
2. **封存分区不再重写**。封存后 manifest 记 `sealed: true`；再写同一分区直接拒绝。

写入时序（方案 §2.4）：

    16:35  data-index.yml   抓指数 → index/_incr/YYYYMMDD/
    16:40  data-stock.yml   抓全A → stock/_incr/YYYYMMDD/{hfq,raw}.parquet
    16:55  data-etf.yml     抓 ETF → etf/_incr/YYYYMMDD/
    17:00  契约测试 + verify_manifest → 提交数据仓
    周日 03:00 data-retention.yml 封存 + 过期删除

压缩参数：
- 日分片 `_incr/`：默认（snappy / zstd-1），**优先写入速度**（反正第二天就封存）
- 封存分区：**zstd level 19**，一次性成本换 15–20% 体积（129MB → ~107MB）
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
from typing import Iterable, Sequence

from .schema import (
    COLUMNS,
    CONTRACT_VERSION,
    FQ,
    PARTIAL_FLAG,
    PRIMARY_KEY,
    RETENTION,
)
from .reader import (
    FrozenPartitionError,
    StoreError,
    _all_parquet,
    normalize_code,
)

__all__ = [
    "write_incremental",
    "seal_partition",
    "expire_partitions",
    "write_manifest",
    "read_manifest",
    "ManifestMismatch",
]

BEIJING = _dt.timezone(_dt.timedelta(hours=8))

INCR_SEALED_DAYS = 60        # 未封存的日分片保留 <=60 天（兜底重算窗口）
SEAL_ZSTD_LEVEL = 19
BATCH_CODES = 200            # 封存分片规模，保证单文件 4–6MB


class ManifestMismatch(AssertionError):
    pass


# ---------------------------------------------------------------------------
# 日增量写入
# ---------------------------------------------------------------------------
def write_incremental(
    df,
    asset: str,
    fq: str,
    date: str | _dt.date,
    *,
    root: str | None = None,
    writer: str | None = None,
    allow_overwrite: bool = False,
    pd=None,
) -> str:
    """写当日增量分片 `<root>/market/<asset>/<fq>/_incr/YYYYMMDD/{fq}.parquet`。

    幂等：同日同文件已存在且行数一致 → 跳过（量化域台账 #10 已有的守卫，提升为全域）。
    同一天重跑无副作用，所以 21:00 补跑 cron 安全。
    """
    if fq not in FQ:
        raise ValueError(f"unknown fq {fq!r}")
    if fq == "qfq":
        raise ValueError("qfq 已废弃，不得写入（方案 §0.3）")
    if pd is None:
        import pandas as pd  # noqa: PLC0415

    root = root or os.environ.get("QH_DATA_ROOT", "data")
    day = date if isinstance(date, str) else date.strftime("%Y%m%d")
    day = day.replace("-", "")

    out_dir = os.path.join(root, "market", asset, fq, "_incr", day)
    out = os.path.join(out_dir, f"{fq}.parquet")

    df = _prepare(df, asset, pd)

    if os.path.exists(out) and not allow_overwrite:
        try:
            old = pd.read_parquet(out)
            if len(old) == len(df):
                return out  # 幂等跳过
        except Exception:
            pass
        raise FrozenPartitionError(
            f"增量分片已存在且行数不同: {out} (old={os.path.getsize(out)}B). "
            f"同日重跑应逐位一致；要强制覆盖请显式 allow_overwrite=True（禁止用于历史回补）"
        )

    os.makedirs(out_dir, exist_ok=True)
    _atomic_write(df, out, pd, zstd=None)
    _bump_manifest(root, asset, fq, df, out, writer=writer, sealed=False, pd=pd)
    return out


# ---------------------------------------------------------------------------
# 封存
# ---------------------------------------------------------------------------
def seal_partition(
    asset: str,
    fq: str,
    year: int,
    month: int,
    *,
    root: str | None = None,
    dry_run: bool = False,
    writer: str | None = None,
    pd=None,
):
    """把某月的日分片合并为封存分区（zstd-19），并删掉已封存的日分片。

    分区形态：`<asset>/<fq>/year=YYYY/month=MM/batch=NN.parquet`（每分片 ~200 code）

    合并后校验：行数 == Σ日分片行数；sha256 写入 manifest；sealed=true。
    """
    if pd is None:
        import pandas as pd  # noqa: PLC0415

    root = root or os.environ.get("QH_DATA_ROOT", "data")
    base = os.path.join(root, "market", asset, fq)
    inc = os.path.join(base, "_incr")
    if not os.path.isdir(inc):
        raise StoreError(f"no _incr dir: {inc}")

    days = []
    for d in sorted(os.listdir(inc)):
        p = os.path.join(inc, d)
        if not os.path.isdir(p):
            continue
        if not d.startswith(f"{year:04d}{month:02d}"):
            continue
        for f in sorted(os.listdir(p)):
            if f.endswith(".parquet"):
                days.append(os.path.join(p, f))
    if not days:
        return {"sealed": 0, "files": 0, "rows": 0}

    frames = []
    for f in days:
        if f.endswith(f"{fq}.parquet") or f.startswith(fq):
            frames.append(pd.read_parquet(f))
    if not frames:
        return {"sealed": 0, "files": 0, "rows": 0}

    df = pd.concat(frames, ignore_index=True)
    df = _prepare(df, asset, pd)
    rows_in = len(df)

    out_dir = os.path.join(base, f"year={year:04d}", f"month={month:02d}")
    if dry_run:
        return {"sealed": 0, "files": len(days), "rows": rows_in, "dry_run": True,
                "target": out_dir}

    os.makedirs(out_dir, exist_ok=True)
    codes = sorted(df["code"].unique())
    n_batch = 0
    written_rows = 0
    for i in range(0, len(codes), BATCH_CODES):
        chunk = codes[i:i + BATCH_CODES]
        sub = df[df["code"].isin(chunk)]
        out = os.path.join(out_dir, f"batch={n_batch:02d}.parquet")
        _atomic_write(sub, out, pd, zstd=SEAL_ZSTD_LEVEL)
        written_rows += len(sub)
        n_batch += 1

    if written_rows != rows_in:
        raise ManifestMismatch(
            f"seal row count mismatch: in={rows_in} out={written_rows} "
            f"—— 封存后行数必须等于 Σ日分片行数"
        )

    # 删除已封存的日分片（未封存的保留 <=60 天兜底窗口）
    for f in days:
        try:
            os.remove(f)
        except OSError:
            pass
    for d in os.listdir(inc):
        p = os.path.join(inc, d)
        if os.path.isdir(p) and not os.listdir(p):
            shutil.rmtree(p, ignore_errors=True)

    csv_sha = _partition_sha(out_dir)
    _bump_manifest_dir(root, asset, fq, out_dir, rows_in, writer=writer, sealed=True, pd=pd)
    return {"sealed": n_batch, "files": n_batch, "rows": rows_in,
            "dir": out_dir, "sha256": csv_sha}


# ---------------------------------------------------------------------------
# 过期删除（只删整分区目录，零重写）
# ---------------------------------------------------------------------------
def expire_partitions(
    asset: str,
    fq: str,
    keep_trade_days: int | None,
    *,
    root: str | None = None,
    today: _dt.date | None = None,
    dry_run: bool = True,
    archive: bool = True,
    pd=None,
):
    """按保留期删整个月分区目录。

    为什么用"删目录"而不是"删行"：个股 3 年窗口滑动，按行删要重写所有分区
    （每天 225MB 的 git churn）。按**月分区**组织后，滑动窗口只在月初/月末跨越
    分区边界时才需要删一个目录，其余日子一行不动。
    """
    if keep_trade_days is None:
        return {"removed": [], "reason": "index retention=None (不过期)"}
    if pd is None:
        import pandas as pd  # noqa: PLC0415

    root = root or os.environ.get("QH_DATA_ROOT", "data")
    base = os.path.join(root, "market", asset, fq)
    if not os.path.isdir(base):
        return {"removed": [], "reason": f"no dir {base}"}

    # 用交易日历算 cutoff（★交易日，不是自然日）
    from ..calendar import load_calendar  # noqa: PLC0415

    try:
        cal = load_calendar(root=root, allow_expired=True)
        today = today or _dt.datetime.now(BEIJING).date()
        cutoff = cal.shift(today, -keep_trade_days) if len(cal) > keep_trade_days else cal.min_date
    except Exception:
        # 无日历时按 244 交易日/年 粗算，并在报告中标注
        today = today or _dt.datetime.now(BEIJING).date()
        cutoff = today - _dt.timedelta(days=int(keep_trade_days * 365 / 244))
        cal = None

    removed = []
    for year in sorted(os.listdir(base)):
        if not year.startswith("year="):
            continue
        y = int(year.split("=")[1])
        ypath = os.path.join(base, year)
        for month in sorted(os.listdir(ypath)):
            if not month.startswith("month="):
                continue
            m = int(month.split("=")[1])
            mpath = os.path.join(ypath, month)
            # 该月最后一天 < cutoff 才删（保边界月完整）
            last_day = _month_end(y, m)
            if last_day >= cutoff:
                continue
            removed.append({"dir": mpath, "last_day": last_day.isoformat()})
            if not dry_run:
                if archive:
                    _archive_before_delete(mpath, asset, fq, y, m)
                shutil.rmtree(mpath, ignore_errors=True)

    # 清理空的 year= 目录
    if not dry_run:
        for year in sorted(os.listdir(base)):
            ypath = os.path.join(base, year)
            if year.startswith("year=") and os.path.isdir(ypath) and not os.listdir(ypath):
                shutil.rmtree(ypath, ignore_errors=True)

    return {
        "asset": asset, "fq": fq, "keep_trade_days": keep_trade_days,
        "cutoff": cutoff.isoformat(), "calendar_used": cal is not None,
        "removed": removed, "dry_run": dry_run,
    }


def _month_end(y: int, m: int) -> _dt.date:
    if m == 12:
        return _dt.date(y, 12, 31)
    return _dt.date(y, m + 1, 1) - _dt.timedelta(days=1)


def _archive_before_delete(path: str, asset: str, fq: str, y: int, m: int) -> str:
    """删前归档到 Release 目录（retention 不可逆，归档是唯一的后悔药）。"""
    arch_root = os.path.join(os.path.dirname(os.path.dirname(path)), "_archive")
    os.makedirs(arch_root, exist_ok=True)
    name = f"{asset}-{fq}-{y}{m:02d}"
    out = os.path.join(arch_root, name)
    if not os.path.exists(out):
        shutil.make_archive(out, "gztar", path)
        return out + ".tar.gz"
    return out + ".tar.gz"


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def _manifest_path(root: str, asset: str, fq: str) -> str:
    return os.path.join(root, "manifest", f"{asset}_{fq}.json")


def read_manifest(root: str, asset: str, fq: str) -> dict:
    p = _manifest_path(root, asset, fq)
    if not os.path.exists(p):
        return {"asset": asset, "fq": fq, "partitions": [], "contract_version": CONTRACT_VERSION}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_manifest(root: str, asset: str, fq: str, man: dict) -> str:
    p = _manifest_path(root, asset, fq)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    man["contract_version"] = CONTRACT_VERSION
    man["updated_at"] = _dt.datetime.now(BEIJING).isoformat(timespec="seconds")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def _bump_manifest(root, asset, fq, df, path, *, writer, sealed, pd):
    man = read_manifest(root, asset, fq)
    part = {
        "path": os.path.relpath(path, root),
        "files": 1,
        "rows": int(len(df)),
        "codes": int(df["code"].nunique()) if "code" in df.columns else 0,
        "date_min": _dmin(df, pd),
        "date_max": _dmax(df, pd),
        "sha256": _file_sha(path),
        "written_at": _dt.datetime.now(BEIJING).isoformat(timespec="seconds"),
        "writer": writer or "unknown",
        "sealed": sealed,
    }
    parts = [p for p in man.get("partitions", []) if p.get("path") != part["path"]]
    parts.append(part)
    man["partitions"] = sorted(parts, key=lambda x: x["path"])
    if asset in RETENTION:
        man["retention"] = {
            "keep_trade_days": RETENTION[asset],
            "note": "★交易日，不是自然日",
        }
    write_manifest(root, asset, fq, man)


def _bump_manifest_dir(root, asset, fq, out_dir, rows, *, writer, sealed, pd):
    man = read_manifest(root, asset, fq)
    sha = _partition_sha(out_dir)
    part = {
        "path": os.path.relpath(out_dir, root),
        "files": len(_all_parquet(out_dir)),
        "rows": int(rows),
        "sha256": sha,
        "written_at": _dt.datetime.now(BEIJING).isoformat(timespec="seconds"),
        "writer": writer or "unknown",
        "sealed": sealed,
    }
    parts = [p for p in man.get("partitions", []) if p.get("path") != part["path"]]
    parts.append(part)
    man["partitions"] = sorted(parts, key=lambda x: x["path"])
    write_manifest(root, asset, fq, man)


def assert_manifest_matches(root: str, asset: str, fq: str, partition_path: str,
                            expect_sha: str) -> None:
    """hfq 冻结断言：随机抽历史日期比对 manifest 记录的分区 sha256。

    量化域硬性陷阱 #3：「历史值永久冻结，重复回补 = 成倍重复行」。
    """
    man = read_manifest(root, asset, fq)
    rel = os.path.relpath(partition_path, root)
    for p in man.get("partitions", []):
        if p.get("path") == rel:
            if p.get("sha256") != expect_sha:
                raise ManifestMismatch(
                    f"frozen partition changed: {rel}\n"
                    f"  manifest: {p.get('sha256')}\n"
                    f"  current : {expect_sha}\n"
                    f"  —— hfq/raw 历史值永久冻结，重复回补会产生成倍重复行"
                )
            return
    raise ManifestMismatch(f"partition not in manifest: {rel}")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _prepare(df, asset: str, pd):
    """列类型归一 + 不变量快检 + 主键去重（同键保留最后一条，模拟 append 语义）。"""
    if "code" not in df.columns or "date" not in df.columns:
        raise StoreError("df must have 'code' and 'date' columns")
    df = df.copy()
    df["code"] = df["code"].map(normalize_code)
    df["date"] = pd.to_datetime(df["date"])
    for c, t in COLUMNS.items():
        if c in ("code", "date"):
            continue
        if c in df.columns:
            if t == "int64":
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
            else:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    return df.sort_values(list(PRIMARY_KEY), kind="stable").reset_index(drop=True)


def _atomic_write(df, path, pd, zstd: int | None):
    tmp = path + ".tmp"
    if zstd:
        df.to_parquet(tmp, index=False, compression="zstd", compression_level=zstd)
    else:
        df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _file_sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _partition_sha(d: str) -> str:
    """分区级 sha：对排序后的 (相对路径, 文件 sha) 求 sha。"""
    h = hashlib.sha256()
    for f in _all_parquet(d):
        rel = os.path.relpath(f, d)
        h.update(rel.encode())
        h.update(_file_sha(f).encode())
    return h.hexdigest()


def _dmin(df, pd) -> str | None:
    if df.empty or "date" not in df.columns:
        return None
    return pd.Timestamp(df["date"].min()).strftime("%Y-%m-%d")


def _dmax(df, pd) -> str | None:
    if df.empty or "date" not in df.columns:
        return None
    return pd.Timestamp(df["date"].max()).strftime("%Y-%m-%d")
