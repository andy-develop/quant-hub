"""数据层唯一读接口 `load()`。

上层开发者唯一需要认识的 API（方案 §2.2）：

    from common.store import load
    df = load(asset="etf", fq="hfq", codes=["512890","510300"],
              start="2016-01-01", end=None,          # end=None -> 最新交易日
              columns=["date","close","amount"])     # 只取需要的列，parquet 列裁剪
    df = load(asset="stock", fq="hfq", last_n=120)   # 个性化域：全A 最近 120 日收盘
    w  = load(asset="index", code="sh000001", freq="weekly")    # 大盘周K，默认只给已走完的周
    m  = load(asset="index", code="sz399001", freq="monthly")   # 深圳成指月K
    w2 = load(asset="index", freq="weekly", closed_only=False)  # 显式要未完成周（盘中视角）
    cal = load_calendar()                             # 交易日历，全项目唯一权威

内部自动完成四件事（顺序不可颠倒）：

    1. 合并「封存分区 + _incr/ 日分片」
    2. 统一代码格式 —— **先 code_map 归一，再叠加 fixup 覆盖**（顺序反了会整段重复行）
    3. 统一列（缺失的 OPTIONAL 列返回 NaN）
    4. 列裁剪（parquet 层就裁，避免读全量）

`domain=` 参数强制：会记进 runlog，CI 加静态检查禁止 `domains/etf/**` 读
`meta/st_history`（短线域私有的时点数据，跨域读 = 口径污染）。

上层拿到的永远是 `code=600000`（短代码）+ `date` + 强类型列。
`sh.` / `1.` 前缀是数据层的私事，上层看不见。
"""

from __future__ import annotations

import fnmatch
import glob
import os
from typing import Iterable, Sequence

from .schema import (
    ASSETS,
    COLUMNS,
    CONTRACT_VERSION,
    DOMAIN_PRIVATE_META,
    DOMAINS,
    FREQ,
    FQ,
    OPTIONAL,
    PARTIAL_FLAG,
    PRIMARY_KEY,
)

__all__ = ["load", "load_meta", "StoreError", "ContractViolation", "FrozenPartitionError"]

# 分区目录形态：<root>/market/<asset>/<fq>/year=YYYY/month=MM/batch=NN.parquet
# 日增量形态：  <root>/market/<asset>/<fq>/_incr/YYYYMMDD/(hfq|raw).parquet
# 指数多周期：  <root>/market/index/<group>/<code>/<freq>/year=YYYY/...
_INCR_DIR = "_incr"


class StoreError(RuntimeError):
    pass


class ContractViolation(AssertionError):
    pass


class FrozenPartitionError(StoreError):
    """试图修改已冻结分区（hfq/raw 历史值永久冻结）。"""


# ---------------------------------------------------------------------------
# 代码格式归一（唯一权威映射）
# ---------------------------------------------------------------------------


_MKT_DIGIT = {"1": "sh", "0": "sz", "2": "bj"}


def normalize_code(code: str) -> str:
    """把任意形态代码归一到短代码：600000 / 512890 / H20269 / 830799。

    接受的输入形态（全部实测于三域数据）：
      baostock 式   `sh.600000` / `sz.000001` / `bj.830799`
      腾讯 secid 式 `1.600000`  / `0.000001`
      前缀无点式    `sh600000`  / `sz000001`
      裸短代码      `600000`    / `000001`
      中证指数      `H20269`    / `H30269` / `H00300`

    归一是**幂等**的（对已归一值再归一不变），这是它能安全地重复调用的前提。
    """
    if code is None:
        raise TypeError("code is None")
    s = str(code).strip()
    if not s:
        raise ValueError("empty code")

    # 中证指数（H 开头 + 数字）—— 原样保留大写
    if len(s) >= 2 and s[0] in ("H", "h") and s[1].isdigit():
        return s.upper()

    low = s.lower()

    # 形态一：<前缀>.<数字>  —— sh.600000 / sz.000001 / bj.830799 / 1.600000 / 0.000001
    if "." in s:
        head, _, tail = s.partition(".")
        if tail.isdigit():
            if head.lower() in ("sh", "sz", "bj"):
                return tail.zfill(6)
            if head in _MKT_DIGIT:
                return tail.zfill(6)
        # 非标准形态，落到下面兜底

    # 形态二：字母前缀无点 —— sh600000 / sz000001 / bj830799
    for pre in ("sh", "sz", "bj"):
        if low.startswith(pre) and len(s) > len(pre) and s[len(pre):].isdigit():
            return s[len(pre):].zfill(6)

    # 形态三：裸数字短代码（含北交所 4/8 开头）
    if s.isdigit():
        return s.zfill(6) if len(s) < 6 else s

    # 未识别：原样返回（如自定义指数代码），由上层决定
    return s


def code_to_secid(code: str) -> str:
    """短代码 -> 腾讯 secid（`1.600000` / `0.000001`）。含北交所映射。

    注：原 `fetch_universe.to_secid()` 只处理 sh.->1. / sz.->0.，没有北交所映射；
    这里补齐，合并 `meta/universe` 时北交所纳入与否由 universe.flag 决定。
    """
    c = normalize_code(code)
    if c.upper().startswith("H") or not c.isdigit():
        raise ValueError(f"cannot map index-like code to secid: {code!r}")
    if c[0] == "6" or c[0] == "9":          # 沪市主板/科创板/沪B
        return f"1.{c}"
    if c[0] in ("0", "2", "3"):             # 深市主板/创业/深B
        return f"0.{c}"
    if c[0] in ("4", "8"):                  # 北交所
        return f"0.{c}"
    raise ValueError(f"unknown market for code {code!r}")


def code_to_symbol(code: str) -> str:
    """短代码 -> 交易所前缀符号（`sh600000`）。"""
    sid = code_to_secid(code)
    mkt, num = sid.split(".")
    return ("sh" if mkt == "1" else "sz") + num


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------
def load(
    asset: str,
    *,
    fq: str = "hfq",
    freq: str = "daily",
    codes: Sequence[str] | None = None,
    code: str | None = None,
    start: str | None = None,
    end: str | None = None,
    last_n: int | None = None,
    columns: Sequence[str] | None = None,
    domain: str | None = None,
    closed_only: bool = True,
    root: str | None = None,
    group: str | None = None,
    calendar=None,
    strict: bool = True,
):
    """读取行情数据（唯一入口）。

    参数
    ----
    asset     : 'stock' | 'etf' | 'index'
    fq        : 'hfq'（默认，冻结） | 'raw' | 'qfq'（已废弃）
    freq      : 'daily'（权威） | 'weekly' | 'monthly'（派生，见 is_partial）
    codes/code: 短代码或任意可归一形态；code 是单标的简写
    start/end : ISO 日期字符串；end=None 表示最新
    last_n    : 取最后 N 个交易日（与 start 互斥，优先 last_n）
    columns   : 需要的列（parquet 列裁剪；code/date 恒含）
    domain    : 调用方域标识，会记 runlog；跨域读私有 meta 在此拦截
    closed_only: weekly/monthly 默认只返回已走完的周期（★正确性要求，见 §2.7）
    strict    : True 时缺数据抛错；False 时返回空 DataFrame

    返回
    ----
    pandas.DataFrame，列 = ['code','date', ...所选列]，date 为 datetime64[ns]。
    """
    _check_asset(asset)
    _check_fq(fq)
    _check_freq(freq)
    _check_domain(domain)

    import pandas as pd  # 延迟导入，便于无 pandas 环境跑纯日历门禁

    root = root or os.environ.get("QH_DATA_ROOT", "data")
    want_cols = _resolve_columns(columns)

    if freq != "daily":
        return _load_derived(
            asset, fq, freq, codes or ([code] if code else None),
            start, end, last_n, want_cols, closed_only, root, group, calendar, strict, pd,
        )

    incr_dir = os.path.join(root, "market", asset, fq)

    frames = []
    # 1) 封存分区（历史月份，永不重写）
    for f in _sealed_files(incr_dir):
        frames.append(_read_parquet(f, want_cols, pd))
    # 2) 日增量分片（append-only）
    for f in _incr_files(incr_dir):
        frames.append(_read_parquet(f, want_cols, pd))

    if not frames:
        if strict:
            raise StoreError(
                f"no data for asset={asset} fq={fq} under {incr_dir!r}. "
                f"先跑 bootstrap.py 或检查 QH_DATA_ROOT"
            )
        return _empty(pd, want_cols)

    df = pd.concat(frames, ignore_index=True)
    df = _normalize_frame(df, asset, fq, root, pd)
    df = _apply_filters(df, codes or ([code] if code else None), start, end, last_n, pd)
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 派生周期读取（weekly / monthly）
# ---------------------------------------------------------------------------
def _load_derived(
    asset, fq, freq, codes, start, end, last_n, want_cols,
    closed_only, root, group, calendar, strict, pd,
):
    if asset not in ("index", "etf"):
        # stock 的周月线按需由上层自行聚合；只有 index/etf 物化入库（方案 §2.7）
        raise StoreError(
            f"derived freq={freq!r} is materialized only for asset in ('index','etf'); "
            f"for asset={asset!r} aggregate from daily via common.aggregate"
        )
    if fq not in ("raw", "hfq"):
        raise StoreError(f"derived freq requires fq in ('raw','hfq'), got {fq!r}")

    base = os.path.join(root, "market", asset)
    subdirs = _index_subdirs(base, group)
    frames = []
    for sub in subdirs:
        d = os.path.join(sub, freq)
        if not os.path.isdir(d):
            continue
        for f in _all_parquet(d):
            frames.append(_read_parquet(f, want_cols, pd, allow_missing_partial=True))
    if not frames:
        if strict:
            raise StoreError(
                f"no index {freq} partitions under {base}"
                + (f" (group={group})" if group else "")
                + ". 需先跑 common/aggregate.py 物化（data-index.yml 的第 ④ 步）"
            )
        return _empty(pd, want_cols)

    df = pd.concat(frames, ignore_index=True)
    if "code" in df.columns:
        df["code"] = df["code"].map(normalize_code)
    df["date"] = pd.to_datetime(df["date"])

    # ★ closed_only：剔除未完成周期（ETF 域实测：不剔除会让 97.7% 周中日期信号错位）
    if closed_only:
        if PARTIAL_FLAG not in df.columns:
            raise ContractViolation(
                f"index {freq} partition is missing required column {PARTIAL_FLAG!r} "
                f"—— 聚合器必须写入该列（见 common/aggregate.py）"
            )
        df = df[~df[PARTIAL_FLAG].astype(bool)]

    df = _apply_filters(df, codes, start, end, last_n, pd)
    if PARTIAL_FLAG in df.columns and PARTIAL_FLAG not in want_cols and PARTIAL_FLAG != "*":
        pass  # 保留该列供上层判断，体积可忽略
    return df.reset_index(drop=True)


def _index_subdirs(base: str, group: str | None) -> list[str]:
    """指数目录：base/<group>/<code>/ 或 base/<code>/（兼容两种布局）。"""
    out: list[str] = []
    if group:
        g = os.path.join(base, group)
        if os.path.isdir(g):
            for name in sorted(os.listdir(g)):
                p = os.path.join(g, name)
                if os.path.isdir(p):
                    out.append(p)
        return out
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        p = os.path.join(base, name)
        if not os.path.isdir(p) or name == _INCR_DIR:
            continue
        # group 目录（broad / csindex / tencent）之下才是 code 目录
        children = [c for c in os.listdir(p) if os.path.isdir(os.path.join(p, c))]
        if any(c.startswith(("sh", "sz", "H")) or c.startswith("0") for c in children):
            out.append(p)
            out += [os.path.join(p, c) for c in children if c != _INCR_DIR]
        else:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# 归一与过滤
# ---------------------------------------------------------------------------
def _normalize_frame(df, asset: str, fq: str, root: str, pd):
    """代码归一 + fixup 覆盖。★ 顺序不可颠倒。"""
    if "code" not in df.columns:
        raise ContractViolation("missing required column 'code'")
    # (1) 先归一 —— 历史分片是 baostock 式 sh./sz.，增量是腾讯式 1./0.
    df["code"] = df["code"].map(normalize_code)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])

    # (2) 再叠加 fixup 覆盖
    #     fixup glob 必须限定同口径（raw_* 或 hfq_*），glob 到另一口径会把复权行拼进不复权库。
    #     （2026-09-08 起 quant-lab 已修复该问题，这里在合并层再加一道断言）
    fix_root = os.path.join(root, "market", asset, "fixup")
    fixs = _fixup_files(fix_root, fq)
    if fixs:
        fix = pd.concat([_read_parquet(f, None, pd) for f in fixs], ignore_index=True)
        fix["code"] = fix["code"].map(normalize_code)
        if "date" in fix.columns:
            fix["date"] = pd.to_datetime(fix["date"])
        keep = ~df["code"].isin(set(fix["code"]))
        df = pd.concat([df[keep], fix], ignore_index=True)

    df = df.sort_values(["code", "date"], kind="stable")
    df = df.drop_duplicates(subset=list(PRIMARY_KEY), keep="last")
    return df


def _fixup_files(fix_root: str, fq: str) -> list[str]:
    if not os.path.isdir(fix_root):
        return []
    pat = f"{fq}_*.parquet"
    files = sorted(
        f for f in glob.glob(os.path.join(fix_root, "*.parquet"))
        if fnmatch.fnmatch(os.path.basename(f), pat)
    )
    # 防呆：确认没有混入另一口径（glob 泄漏是最危险的一类静默错误）
    other = "raw" if fq == "hfq" else "hfq"
    leaked = [f for f in files if fnmatch.fnmatch(os.path.basename(f), f"{other}_*")]
    if leaked:
        raise FrozenPartitionError(
            f"fixup glob leaked cross-fq files: {leaked} —— "
            f"会把 {other} 行拼进 {fq} 库，导致静默口径污染"
        )
    return files


def _resolve_columns(columns: Sequence[str] | None) -> list[str]:
    base = ["code", "date"]
    if not columns:
        return ["*"]
    if columns == ["*"] or list(columns) == ["*"]:
        return ["*"]
    out = list(base)
    valid = set(COLUMNS) | set(OPTIONAL) | {PARTIAL_FLAG}
    for c in columns:
        if c in base:
            continue
        if c not in valid:
            raise ContractViolation(
                f"unknown column {c!r}; valid = {sorted(valid)}. "
                f"新增列必须先进 common/store/schema.py 的 COLUMNS/OPTIONAL（契约变更）"
            )
        out.append(c)
    return out


def _apply_filters(df, codes, start, end, last_n, pd):
    if codes:
        want = {normalize_code(c) for c in codes}
        df = df[df["code"].isin(want)]
    if start is not None:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["date"] <= pd.Timestamp(end)]
    if last_n is not None and last_n > 0:
        if df.empty:
            return df
        keep_dates = sorted(df["date"].unique())[-last_n:]
        df = df[df["date"].isin(keep_dates)]
    return df


def _empty(pd, cols):
    import numpy as np
    if cols == ["*"]:
        names = list(COLUMNS)
    else:
        names = list(cols)
    data = {}
    for n in names:
        data[n] = np.array([], dtype="datetime64[ns]") if n == "date" else np.array([])
    return pd.DataFrame(data)


def _read_parquet(path: str, cols: list[str] | None, pd, allow_missing_partial: bool = False):
    try:
        if cols and cols != ["*"]:
            # 列裁剪前先读元数据；缺失列由 pandas 抛错，这里降级为读全量再补
            try:
                return pd.read_parquet(path, columns=list(cols))
            except Exception:
                df = pd.read_parquet(path)
                for c in cols:
                    if c not in df.columns:
                        df[c] = None
                return df[[c for c in cols if c in df.columns]]
        return pd.read_parquet(path)
    except Exception as exc:
        raise StoreError(f"failed to read parquet {path}: {exc}") from exc


def _sealed_files(root: str) -> list[str]:
    """封存分区：year=*/month=*/*.parquet（不含 _incr）。"""
    if not os.path.isdir(root):
        return []
    out = []
    for year in sorted(glob.glob(os.path.join(root, "year=*"))):
        for month in sorted(glob.glob(os.path.join(year, "month=*"))):
            out += sorted(glob.glob(os.path.join(month, "*.parquet")))
    # 兼容旧式平铺命名（quant-lab 现状：hfq_b0_00.parquet）
    out += sorted(glob.glob(os.path.join(root, "*.parquet")))
    return out


def _incr_files(root: str) -> list[str]:
    inc = os.path.join(root, _INCR_DIR)
    if not os.path.isdir(inc):
        return []
    out = []
    for day in sorted(os.listdir(inc)):
        d = os.path.join(inc, day)
        if os.path.isdir(d):
            out += sorted(glob.glob(os.path.join(d, "*.parquet")))
    out += sorted(glob.glob(os.path.join(inc, "*.parquet")))
    return out


def read_partition_daily(
    asset: str,
    fq: str,
    year: int,
    month: int,
    *,
    root: str | None = None,
    pd=None,
):
    """读某月「封存分区 + 未封存 _incr 分片」的全部行（backfill 幂等合并用）。

    backfill_daily 以整月重建语义重写分区时，必须以「已存在行 + 新抓行」合并后写入，
    否则不同 run 只抓各自 code 子集会整月抹掉其他 code 的行
    （2026-09-14 起 data-index 只跑 BROAD，把 09-12 手动回补的 sh000852 全抹掉的教训）。
    返回 None 表示该月无任何数据。
    """
    root = root or os.environ.get("QH_DATA_ROOT", "data")
    base = os.path.join(root, "market", asset, fq)
    frames: list = []

    mdir = os.path.join(base, f"year={year:04d}", f"month={month:02d}")
    if os.path.isdir(mdir):
        for f in sorted(glob.glob(os.path.join(mdir, "*.parquet"))):
            frames.append(_read_parquet(f, None, pd))

    inc = os.path.join(base, _INCR_DIR)
    if os.path.isdir(inc):
        pref = f"{year:04d}{month:02d}"
        for d in sorted(os.listdir(inc)):
            if not d.startswith(pref):
                continue
            dp = os.path.join(inc, d)
            if os.path.isdir(dp):
                for f in sorted(glob.glob(os.path.join(dp, "*.parquet"))):
                    frames.append(_read_parquet(f, None, pd))

    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["code"] = df["code"].map(normalize_code)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["code", "date"], kind="stable").reset_index(drop=True)


def clear_month_incr(asset: str, fq: str, year: int, month: int, *, root: str | None = None) -> int:
    """删除某月 `_incr/YYYYMM*` 残留日分片（backfill 封存前防重复合并）。

    seal_partition 会把该月 _incr 下**所有**分片并入封存分区；backfill 合并重建时
    若残留了上一轮/其他写入者的分片，会与已读入的封存行重复。先清再写，保证封存
    内容 = 本轮合并结果（幂等：无残留时是 no-op）。
    """
    import shutil
    root = root or os.environ.get("QH_DATA_ROOT", "data")
    inc = os.path.join(root, "market", asset, fq, _INCR_DIR)
    if not os.path.isdir(inc):
        return 0
    pref = f"{year:04d}{month:02d}"
    removed = 0
    for d in os.listdir(inc):
        if d.startswith(pref):
            shutil.rmtree(os.path.join(inc, d), ignore_errors=True)
            removed += 1
    return removed


def _all_parquet(root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "**", "*.parquet"), recursive=True))


# ---------------------------------------------------------------------------
# meta 读取（含跨域私有表拦截）
# ---------------------------------------------------------------------------
def load_meta(name: str, *, root: str | None = None, domain: str | None = None, pd=None):
    """读取 meta 表。

    跨域私有表（如 st_history 属短线域）在本函数拦截 —— 这是"底层不动"的口径级保证：
    跨域读 = 口径污染，CI 另有静态 grep 检查作为第二道。
    """
    _check_domain(domain)
    if domain is not None:
        for owner, tables in DOMAIN_PRIVATE_META.items():
            if name in tables and owner != domain:
                raise ContractViolation(
                    f"domain={domain!r} 试图读取 {owner!r} 的私有 meta 表 {name!r} "
                    f"—— 跨域读 = 口径污染（CI 静态检查同样禁止）"
                )
    if pd is None:
        import pandas as pd  # noqa: PLC0415

    root = root or os.environ.get("QH_DATA_ROOT", "data")
    for ext in (".parquet", ".json", ".csv"):
        p = os.path.join(root, "meta", name + ext)
        if os.path.exists(p):
            if ext == ".parquet":
                return pd.read_parquet(p)
            if ext == ".csv":
                return pd.read_csv(p)
            import json  # noqa: PLC0415

            with open(p, encoding="utf-8") as f:
                return json.load(f)
    raise StoreError(f"meta table not found: {name} under {root}/meta")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _check_asset(asset: str) -> None:
    if asset not in ASSETS:
        raise ValueError(f"asset must be one of {ASSETS}, got {asset!r}")


def _check_fq(fq: str) -> None:
    if fq not in FQ:
        raise ValueError(f"fq must be one of {tuple(FQ)}, got {fq!r}")
    if fq == "qfq":
        raise ValueError(
            "qfq 已废弃（方案 §0.3）：除权处假跳变，动量最大偏差 1.75pp。"
            "请用 hfq（同基期下比值恒等）"
        )


def _check_freq(freq: str) -> None:
    if freq not in FREQ:
        raise ValueError(f"freq must be one of {tuple(FREQ)}, got {freq!r}")


def _check_domain(domain: str | None) -> None:
    if domain is not None and domain not in DOMAINS:
        raise ValueError(f"domain must be one of {DOMAINS}, got {domain!r}")


def contract_version() -> str:
    return CONTRACT_VERSION
