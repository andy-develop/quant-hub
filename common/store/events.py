"""事件型数据读写接口（独立于 K 线 asset/fq 体系）。

事件型数据（龙虎榜 / 涨停复盘）主键不是 (code, date)，不能进 market/<asset> 分区，
统一放 `data/events/<table>/`，表定义在 schema.EVENT_TABLES：

    封存分区：  events/<table>/year=YYYY/month=MM/batch=NN.parquet（zstd-19）
    日增量：    events/<table>/_incr/YYYYMMDD/<table>.parquet
    manifest：  data/manifest/events_<table>.json

写入语义（事件型 ≠ hfq 冻结，但同日重跑必须幂等）：
  - 日增量已存在且行数一致  -> 跳过
  - 日增量已存在但行数不同  -> 按主键合并去重后重写（覆盖；防抓取中途失败/缺席位的二次抓取）
  - 整月封存                -> 整月重建语义：先删旧分区目录再写（backfill 幂等）

读取：load_events() 合并「封存分区 + _incr 日分片」，按主键去重，支持按日期/代码过滤。
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import os
from typing import Sequence

from .schema import EVENT_TABLES, CONTRACT_VERSION

__all__ = ["write_event", "write_event_month", "load_events", "read_event_manifest",
           "check_event_table"]

BEIJING = _dt.timezone(_dt.timedelta(hours=8))
EVENT_DIR = "events"
SEAL_ZSTD_LEVEL = 19


class EventStoreError(RuntimeError):
    pass


def _table(name: str) -> dict:
    if name not in EVENT_TABLES:
        raise ValueError(f"unknown event table {name!r}, expected one of {sorted(EVENT_TABLES)}")
    return EVENT_TABLES[name]


def _normalize(df, name: str, pd):
    """列类型归一 + 主键去重（同键保留最后一条，模拟 append 语义）。

    类型映射：int64/float64 -> 强转数值（NaN 补 0/保留）；string[pyarrow] 等字符串列
    保留文本，绝不做数值化（否则 name/reason/seat_name 会被 to_numeric 打成 NaN）。
    """
    spec = _table(name)
    if df is None or df.empty:
        return df
    missing = [k for k in spec["primary_key"] if k not in df.columns]
    if missing:
        raise ValueError(
            f"[events/{name}] 缺主键列 {missing}（表定义 {spec['primary_key']}），"
            "请检查抓取字段映射")
    df = df.copy()
    if "code" in df.columns:
        from .reader import normalize_code
        df["code"] = df["code"].map(normalize_code)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    for c, t in spec["columns"].items():
        if c not in df.columns or c in ("code", "date"):
            continue
        if t == "int64":
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
        elif t == "float64":
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        else:  # string[pyarrow] 字符串列：保留文本
            df[c] = df[c].where(df[c].notna(), None).astype(t)
    return (df.drop_duplicates(subset=list(spec["primary_key"]), keep="last")
              .sort_values(list(spec["primary_key"]), kind="stable")
              .reset_index(drop=True))


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
    import glob
    h = hashlib.sha256()
    for f in sorted(glob.glob(os.path.join(d, "**", "*.parquet"), recursive=True)):
        rel = os.path.relpath(f, d)
        h.update(rel.encode())
        h.update(_file_sha(f).encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# manifest（events_<name>.json，结构与 writer 的 asset/fq manifest 一致）
# ---------------------------------------------------------------------------
def _manifest_path(root: str, name: str) -> str:
    return os.path.join(root, "manifest", f"events_{name}.json")


def read_event_manifest(root: str, name: str) -> dict:
    p = _manifest_path(root, name)
    if not os.path.exists(p):
        return {"table": name, "partitions": [], "contract_version": CONTRACT_VERSION}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _write_event_manifest(root: str, name: str, man: dict) -> str:
    p = _manifest_path(root, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    man["contract_version"] = CONTRACT_VERSION
    man["updated_at"] = _dt.datetime.now(BEIJING).isoformat(timespec="seconds")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def _bump_manifest(root, name, df, path, *, writer, sealed, pd):
    man = read_event_manifest(root, name)
    if os.path.isdir(path):  # 整月封存分区目录：目录指纹
        sha = _partition_sha(path)
        n_files = len(glob.glob(os.path.join(path, "**", "*.parquet"), recursive=True))
    else:                     # 单日增量文件：文件指纹
        sha = _file_sha(path) if os.path.exists(path) else None
        n_files = 1 if sha else 0
    part = {
        "path": os.path.relpath(path, root),
        "files": n_files,
        "rows": int(len(df)),
        "sha256": sha,
        "date_min": _dmin(df, pd),
        "date_max": _dmax(df, pd),
        "written_at": _dt.datetime.now(BEIJING).isoformat(timespec="seconds"),
        "writer": writer or "unknown",
        "sealed": sealed,
    }
    parts = [p for p in man.get("partitions", []) if p.get("path") != part["path"]]
    parts.append(part)
    man["partitions"] = sorted(parts, key=lambda x: x["path"])
    _write_event_manifest(root, name, man)


def _dmin(df, pd) -> str | None:
    if df.empty or "date" not in df.columns:
        return None
    return pd.Timestamp(df["date"].min()).strftime("%Y-%m-%d")


def _dmax(df, pd) -> str | None:
    if df.empty or "date" not in df.columns:
        return None
    return pd.Timestamp(df["date"].max()).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def write_event(df, name: str, day, *, root: str | None = None, writer: str | None = None,
                pd=None) -> str:
    """写当日增量分片 `events/<name>/_incr/YYYYMMDD/<name>.parquet`。

    幂等：同日同文件已存在且行数一致 -> 跳过；不一致 -> 主键合并去重后重写。
    """
    _table(name)
    if pd is None:
        import pandas as pd  # noqa: PLC0415
    root = root or os.environ.get("QH_DATA_ROOT", "data")
    day_s = day if isinstance(day, str) else day.strftime("%Y%m%d")
    day_s = day_s.replace("-", "")

    out_dir = os.path.join(root, EVENT_DIR, name, "_incr", day_s)
    out = os.path.join(out_dir, f"{name}.parquet")

    df = _normalize(df, name, pd)
    if os.path.exists(out):
        try:
            old = pd.read_parquet(out)
            if len(old) == len(df):
                return out  # 幂等跳过
        except Exception:  # noqa: BLE001
            pass
        # 行数不同：合并去重重写（事件型无冻结语义，二次抓取允许修正）
        old = pd.read_parquet(out)
        merged = pd.concat([old, df], ignore_index=True)
        merged = _normalize(merged, name, pd)
        if merged.empty:
            return out
        df = merged

    os.makedirs(out_dir, exist_ok=True)
    _atomic_write(df, out, pd, zstd=None)
    _bump_manifest(root, name, df, out, writer=writer, sealed=False, pd=pd)
    return out


def write_event_month(df, name: str, year: int, month: int, *, root: str | None = None,
                      writer: str | None = None, pd=None) -> dict:
    """整月封存 `events/<name>/year=YYYY/month=MM/batch=00.parquet`（zstd-19）。

    整月重建语义：先删旧分区目录再写，backfill 幂等（git diff 无变化 = 无需提交）。
    """
    _table(name)
    if pd is None:
        import pandas as pd  # noqa: PLC0415
    root = root or os.environ.get("QH_DATA_ROOT", "data")
    base = os.path.join(root, EVENT_DIR, name)
    out_dir = os.path.join(base, f"year={year:04d}", f"month={month:02d}")

    df = _normalize(df, name, pd)
    if df.empty:
        return {"table": name, "rows": 0, "dir": out_dir, "skipped": True}
    if os.path.isdir(out_dir):
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "batch=00.parquet")
    _atomic_write(df, out, pd, zstd=SEAL_ZSTD_LEVEL)
    _bump_manifest(root, name, df, out_dir, writer=writer, sealed=True, pd=pd)
    # 整月重建语义：该月 _incr 日分片已并入整月分区，清理防 load_events 重复
    # （_incr 目录名是完整日期 YYYYMMDD，用 YYYYMM 前缀 glob 匹配该月所有日）
    inc_prefix = os.path.join(base, "_incr", f"{year:04d}{month:02d}")
    import shutil as _sh
    removed = []
    for _d in glob.glob(inc_prefix + "*"):
        if os.path.isdir(_d):
            _sh.rmtree(_d, ignore_errors=True)
            removed.append(os.path.relpath(_d, root))
    # manifest 同步：移除该月已清理的 _incr 条目（避免 verify 报"分区路径不存在"）
    if removed:
        man = read_event_manifest(root, name)
        man["partitions"] = [p for p in man.get("partitions", [])
                             if p.get("path") not in removed]
        _write_event_manifest(root, name, man)
    return {"table": name, "rows": int(len(df)), "dir": out_dir,
            "sha256": _partition_sha(out_dir)}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def load_events(name: str, *, start: str | None = None, end: str | None = None,
                dates: Sequence[str] | None = None, codes: Sequence[str] | None = None,
                columns: Sequence[str] | None = None, root: str | None = None,
                strict: bool = True, pd=None):
    """读取事件型数据（唯一入口）。

    参数
    ----
    name    : 表名（schema.EVENT_TABLES 键，如 lhb_detail / zt_daily）
    dates   : 具体日期列表（YYYY-MM-DD 或 YYYYMMDD），"按任意历史日期查"直通参数
    start/end: ISO 日期范围（与 dates 可叠加）
    codes   : 短代码过滤
    columns : 需要的列（parquet 列裁剪；date 恒含）
    strict  : True 时表无数据抛错；False 返回空 DataFrame

    返回 pandas.DataFrame，date 为 datetime64[ns]。
    """
    _table(name)
    if pd is None:
        import pandas as pd  # noqa: PLC0415
    root = root or os.environ.get("QH_DATA_ROOT", "data")
    base = os.path.join(root, EVENT_DIR, name)

    # date 恒含（读取列裁剪强制前缀 ["date"] + want）；若调用方 columns 里带了
    # date，剔除之，否则前缀拼接产生重复 date 列 -> df["date"] 变 DataFrame
    # -> pd.to_datetime 报 cannot assemble with duplicate keys
    want = [c for c in columns if c != "date"] if columns else None
    frames = []
    for p in _event_files(base):
        try:
            if want:
                try:
                    frames.append(pd.read_parquet(p, columns=["date"] + want))
                except Exception:  # noqa: BLE001
                    d = pd.read_parquet(p)
                    for c in want:
                        if c not in d.columns:
                            d[c] = None
                    frames.append(d[["date"] + [c for c in want if c in d.columns]])
            else:
                frames.append(pd.read_parquet(p))
        except Exception as exc:  # noqa: BLE001
            raise EventStoreError(f"failed to read event parquet {p}: {exc}") from exc
    if not frames:
        if strict:
            raise EventStoreError(f"no data for event table {name!r} under {base!r}")
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = _normalize(df, name, pd)
    if "date" in df.columns:
        d = pd.to_datetime(df["date"]).dt.date
        if dates:
            want_d = {_to_date(x) for x in dates}
            df = df[d.isin(want_d)]
            d = pd.to_datetime(df["date"]).dt.date  # 过滤后刷新，避免布尔索引 reindex
        if start is not None:
            df = df[d >= _to_date(start)]
            d = pd.to_datetime(df["date"]).dt.date
        if end is not None:
            df = df[d <= _to_date(end)]
    if codes and "code" in df.columns:
        from .reader import normalize_code
        want_c = {normalize_code(c) for c in codes}
        df = df[df["code"].isin(want_c)]
    return df.reset_index(drop=True)


def _to_date(x) -> _dt.date:
    if isinstance(x, _dt.date):
        return x
    s = str(x).replace("-", "")
    return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _event_files(base: str):
    """封存分区 + _incr 日分片（与 reader._sealed_files/_incr_files 同形态）。"""
    import glob
    out = []
    for year in sorted(glob.glob(os.path.join(base, "year=*"))):
        for month in sorted(glob.glob(os.path.join(year, "month=*"))):
            out += sorted(glob.glob(os.path.join(month, "*.parquet")))
    inc = os.path.join(base, "_incr")
    if os.path.isdir(inc):
        for day in sorted(os.listdir(inc)):
            d = os.path.join(inc, day)
            if os.path.isdir(d):
                out += sorted(glob.glob(os.path.join(d, "*.parquet")))
    return out


# ---------------------------------------------------------------------------
# 校验（verify.py 的 check_events 复用）
# ---------------------------------------------------------------------------
def check_event_table(root: str, name: str, pd, problems: list[str], calendar=None) -> int:
    """契约校验：列 ⊆ EVENT_TABLES 定义、含主键、主键无重复、日期在日历内。返回分区数。"""
    spec = _table(name)
    n = 0
    for p in _event_files(os.path.join(root, EVENT_DIR, name)):
        n += 1
        try:
            df = pd.read_parquet(p)
        except Exception as e:  # noqa: BLE001
            problems.append(f"[events/{name}] 读取失败 {p}: {e}")
            continue
        allowed = set(spec["columns"]) | {"period_key", "_degraded"}
        extra = set(df.columns) - allowed
        if extra:
            problems.append(f"[events/{name}] {p}: 未知列 {sorted(extra)}")
        for k in spec["primary_key"]:
            if k not in df.columns:
                problems.append(f"[events/{name}] {p}: 缺主键列 {k}")
                return n
        dup = df.duplicated(subset=list(spec["primary_key"])).sum()
        if dup:
            problems.append(f"[events/{name}] {p}: {int(dup)} 个重复主键")
        if calendar is not None and "date" in df.columns:
            try:
                lo, hi = calendar.min_date, calendar.max_date
                days = [d for d in pd.to_datetime(df["date"]).dt.date.unique() if lo <= d <= hi]
                off = [d for d in days if not calendar.is_trading_day(d)]
                if off:
                    problems.append(f"[events/{name}] {p}: {len(off)} 个非交易日日期，示例 {off[:3]}")
            except Exception:  # noqa: BLE001
                pass
    return n
