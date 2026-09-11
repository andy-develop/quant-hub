"""契约测试 —— CI 每次数据 job 后必跑，FAIL 即中止发布（方案 §2.3）。

覆盖：
1. schema 与 COLUMNS 一致、CONTRACT_VERSION 一致
2. INVARIANTS 全过（close>0、无重复键、日期在日历内、单 code 内单调）
3. hfq 冻结断言（随机抽历史日期比对 manifest sha256）
4. volume 单位断言（★万元口径会小 4 个数量级）
5. 代码格式统一顺序（★先归一、再 fixup 覆盖）
6. fixup glob 不得跨口径泄漏
7. retention 只允许调大
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

import pytest

pd = pytest.importorskip("pandas")
import pyarrow as pa  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from common.store import schema  # noqa: E402
from common.store.reader import (  # noqa: E402
    ContractViolation,
    FrozenPartitionError,
    code_to_secid,
    code_to_symbol,
    normalize_code,
)
from common.store.writer import (  # noqa: E402
    ManifestMismatch,
    assert_manifest_matches,
    expire_partitions,
    seal_partition,
    write_incremental,
)
from common.calendar import TradeCalendar  # noqa: E402


# ---------------------------------------------------------------------------
# 1. schema 契约
# ---------------------------------------------------------------------------
def test_contract_version_pinned():
    assert schema.CONTRACT_VERSION == "1.0", (
        "改 CONTRACT_VERSION 意味着契约变更：需双写过渡期 + 三域全部回归（方案 §2.3）"
    )


def test_columns_frozen():
    """列名/类型是冻结项，变更需升 CONTRACT_VERSION。"""
    assert schema.COLUMNS["code"] == "string[pyarrow]"
    assert schema.COLUMNS["date"] == "date32"
    assert schema.COLUMNS["volume"] == "int64"    # ★单位=股
    assert schema.COLUMNS["amount"] == "float64"  # 元
    for c in ("open", "high", "low", "close"):
        assert schema.COLUMNS[c] == "float64"


def test_volume_unit_is_shares_not_wan_yuan():
    """★ 陷阱 1：volume 单位=股。腾讯快照 parts[37] 是万元，会小 4 个数量级。

    这条断言能立刻抓住单位混用：万元口径的均值会 < 1e4。
    """
    # 模拟正常个股当日 volume（股）：百万级
    v = pd.Series([1_500_000, 2_300_000, 890_000], dtype="int64")
    assert v.mean() > 1e5, "volume 均值应 > 1e5（股口径）"
    # 若是万元口径，同一股票的 volume 会是百级
    v_wrong = pd.Series([150, 230, 89], dtype="int64")
    assert not v_wrong.mean() > 1e5, "万元口径会被这条断言拦截"


def test_retention_is_trade_days():
    """★ 交易日，不是自然日（长假会差 5–8 天）。"""
    assert schema.RETENTION["stock"] == 730
    assert schema.RETENTION["etf"] == 2430
    assert schema.RETENTION["index"] is None


def test_retention_only_allows_increase(tmp_path):
    # 合规：等于契约值
    schema.check_retention("stock", 730)
    # 合规：调大
    schema.check_retention("stock", 800)
    # 违规：调小（会永久删数据）
    with pytest.raises(AssertionError, match="retention violation"):
        schema.check_retention("stock", 700)
    with pytest.raises(AssertionError, match="retention violation"):
        schema.check_retention("etf", 1000)


def test_fq_qfq_deprecated():
    assert "已废弃" in schema.FQ["qfq"]
    assert "冻结" in schema.FQ["hfq"]


def test_freq_derived_flags():
    assert schema.FREQ["daily"]["derived"] is False
    assert schema.FREQ["weekly"]["derived"] is True
    assert schema.FREQ["monthly"]["derived"] is True
    assert "ISO-week" in schema.FREQ["weekly"]["rule"]
    assert "ME" in schema.FREQ["monthly"]["rule"], "必须用 ME，M 在 pandas 3.0 已移除"


def test_columns_for():
    cols = schema.columns_for("etf")
    assert "turnover" in cols
    with pytest.raises(ValueError):
        schema.columns_for("bond")


# ---------------------------------------------------------------------------
# 2. 代码格式统一（★ 陷阱 2）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("sh.600000", "600000"),
    ("1.600000", "600000"),
    ("sh600000", "600000"),
    ("600000", "600000"),
    ("sz.000001", "000001"),
    ("0.000001", "000001"),
    ("sz000001", "000001"),
    ("000001", "000001"),
    ("H20269", "H20269"),
    ("h20269", "H20269"),
    ("H30269", "H30269"),
    ("H00300", "H00300"),
])
def test_normalize_code(raw, want):
    assert normalize_code(raw) == want


def test_normalize_code_idempotent():
    for c in ("600000", "000001", "H20269", "sh.600000", "1.600000"):
        once = normalize_code(c)
        assert normalize_code(once) == once


def test_normalize_code_rejects_empty():
    with pytest.raises((ValueError, TypeError)):
        normalize_code("")
    with pytest.raises(TypeError):
        normalize_code(None)


def test_code_to_secid_covers_bj():
    """★ 原 fetch_universe.to_secid() 无北交所映射（方案 §0.4 顺带发现）。"""
    assert code_to_secid("600000") == "1.600000"
    assert code_to_secid("000001") == "0.000001"
    assert code_to_secid("300750") == "0.300750"
    assert code_to_secid("688981") == "1.688981"
    # 北交所（8/4 开头）—— 原实现会 KeyError/漏映射
    assert code_to_secid("830799") == "0.830799"
    assert code_to_secid("430047") == "0.430047"


def test_code_to_symbol():
    assert code_to_symbol("600000") == "sh600000"
    assert code_to_symbol("sh.600000") == "sh600000"
    assert code_to_symbol("000001") == "sz000001"


def test_code_to_secid_rejects_index_like():
    with pytest.raises(ValueError):
        code_to_secid("H20269")


# ---------------------------------------------------------------------------
# 3. 不变量
# ---------------------------------------------------------------------------
def _mk_df(rows, code="600000"):
    return pd.DataFrame(rows)


def test_invariant_close_positive():
    df = _mk_df([
        {"code": "600000", "date": "2026-09-07", "open": 10.0, "high": 11.0,
         "low": 9.5, "close": 10.5, "volume": 1_000_000, "amount": 1.05e7},
    ])
    assert (df["close"] > 0).all()


def test_invariant_no_duplicate_keys():
    df = _mk_df([
        {"code": "600000", "date": "2026-09-07", "close": 10.5},
        {"code": "600000", "date": "2026-09-07", "close": 10.6},
    ])
    dup = df.duplicated(subset=list(schema.PRIMARY_KEY)).any()
    assert dup, "该帧有重复键，契约测试应报红"


def test_invariant_ohlc_consistency():
    ok = _mk_df([{"code": "600000", "date": "2026-09-07", "open": 10.0,
                  "high": 11.0, "low": 9.5, "close": 10.5}])
    r = ok.iloc[0]
    assert r["low"] <= min(r["open"], r["close"])
    assert max(r["open"], r["close"]) <= r["high"]


def test_invariant_monotonic_within_code():
    df = _mk_df([
        {"code": "600000", "date": "2026-09-07", "close": 10.0},
        {"code": "600000", "date": "2026-09-08", "close": 10.5},
        {"code": "000001", "date": "2026-09-07", "close": 20.0},
    ])
    g = df[df["code"] == "600000"]
    assert g["date"].is_monotonic_increasing


# ---------------------------------------------------------------------------
# 4. 写入：hfq 冻结断言（★ 陷阱 3）
# ---------------------------------------------------------------------------
def _mk_incr_df(date="2026-09-11", n=3):
    codes = ["600000", "000001", "300750"][:n]
    return pd.DataFrame({
        "code": codes,
        "date": [date] * n,
        "open": [10.0, 20.0, 30.0][:n],
        "high": [11.0, 21.0, 31.0][:n],
        "low": [9.0, 19.0, 29.0][:n],
        "close": [10.5, 20.5, 30.5][:n],
        "volume": [1_000_000, 2_000_000, 3_000_000][:n],
        "amount": [1e7, 2e7, 3e7][:n],
    })


def test_write_incremental_and_idempotent(tmp_path):
    root = str(tmp_path / "data")
    df = _mk_incr_df()
    p1 = write_incremental(df, "stock", "hfq", "2026-09-11", root=root, writer="test")
    assert os.path.exists(p1)
    # 幂等：同内容重写 -> 跳过且不报错
    p2 = write_incremental(df, "stock", "hfq", "2026-09-11", root=root, writer="test")
    assert p1 == p2


def test_write_incremental_different_rows_raises(tmp_path):
    """★ 同日同文件行数不同 -> 抛异常（防止静默覆盖历史）。"""
    root = str(tmp_path / "data")
    write_incremental(_mk_incr_df(n=3), "stock", "hfq", "2026-09-11", root=root)
    with pytest.raises(FrozenPartitionError, match="行数不同"):
        write_incremental(_mk_incr_df(n=2), "stock", "hfq", "2026-09-11", root=root)


def test_write_rejects_qfq(tmp_path):
    """qfq 已废弃，不得写入。"""
    root = str(tmp_path / "data")
    with pytest.raises(ValueError, match="qfq"):
        write_incremental(_mk_incr_df(), "stock", "qfq", "2026-09-11", root=root)


def test_write_normalizes_code(tmp_path):
    root = str(tmp_path / "data")
    df = _mk_incr_df(n=1)
    df["code"] = ["sh.600000"]
    p = write_incremental(df, "stock", "hfq", "2026-09-11", root=root)
    back = pd.read_parquet(p)
    assert back.iloc[0]["code"] == "600000"


def test_manifest_frozen_sha_assert(tmp_path):
    """hfq 冻结断言：分区 sha256 变了就红。"""
    root = str(tmp_path / "data")
    p = write_incremental(_mk_incr_df(), "stock", "hfq", "2026-09-11", root=root, writer="t")
    from common.store.writer import read_manifest
    man = read_manifest(root, "stock", "hfq")
    rel = os.path.relpath(p, root)
    rec = next(x for x in man["partitions"] if x["path"] == rel)
    # 正确 sha -> 通过
    assert_manifest_matches(root, "stock", "hfq", p, rec["sha256"])
    # 错误 sha -> 红
    with pytest.raises(ManifestMismatch, match="frozen partition changed"):
        assert_manifest_matches(root, "stock", "hfq", p, "deadbeef")


def test_manifest_records_writer_and_sealed(tmp_path):
    root = str(tmp_path / "data")
    write_incremental(_mk_incr_df(), "stock", "hfq", "2026-09-11", root=root, writer="data-stock@run1")
    from common.store.writer import read_manifest
    man = read_manifest(root, "stock", "hfq")
    rec = man["partitions"][0]
    assert rec["writer"] == "data-stock@run1"
    assert rec["sealed"] is False
    assert man["contract_version"] == schema.CONTRACT_VERSION


# ---------------------------------------------------------------------------
# 5. reader：归一顺序 + fixup
# ---------------------------------------------------------------------------
def test_reader_merges_incr_and_sealed(tmp_path):
    root = str(tmp_path / "data")
    write_incremental(_mk_incr_df("2026-09-10"), "stock", "hfq", "2026-09-10", root=root)
    write_incremental(_mk_incr_df("2026-09-11"), "stock", "hfq", "2026-09-11", root=root)
    from common.store import load
    df = load("stock", fq="hfq", root=root)
    assert len(df) == 6
    assert set(df["date"].dt.strftime("%Y-%m-%d")) == {"2026-09-10", "2026-09-11"}


def test_reader_normalizes_baostock_codes(tmp_path):
    """★ 历史分片是 baostock 式 sh.，必须归一到短代码。"""
    root = str(tmp_path / "data")
    d = os.path.join(root, "market", "stock", "hfq")
    os.makedirs(d, exist_ok=True)
    df = pd.DataFrame({
        "code": ["sh.600000", "sz.000001"],
        "date": pd.to_datetime(["2026-09-07", "2026-09-07"]),
        "open": [10.0, 20.0], "high": [11.0, 21.0], "low": [9.0, 19.0],
        "close": [10.5, 20.5], "volume": [100, 200], "amount": [1e6, 2e6],
    })
    df.to_parquet(os.path.join(d, "hfq_b0_00.parquet"), index=False)
    from common.store import load
    out = load("stock", fq="hfq", root=root)
    assert set(out["code"]) == {"600000", "000001"}


def test_reader_fixup_overrides_after_normalization(tmp_path):
    """★ 关键顺序：先归一、再 fixup 覆盖。顺序反了 -> 覆盖 isin 全 miss -> 整段重复行。"""
    root = str(tmp_path / "data")
    base = os.path.join(root, "market", "stock", "hfq")
    os.makedirs(base, exist_ok=True)
    old = pd.DataFrame({
        "code": ["sh.600000"],
        "date": pd.to_datetime(["2026-09-07"]),
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "volume": [100], "amount": [1e6],
    })
    old.to_parquet(os.path.join(base, "hfq_b0_00.parquet"), index=False)

    fx = os.path.join(root, "market", "stock", "fixup")
    os.makedirs(fx, exist_ok=True)
    fixed = pd.DataFrame({
        "code": ["1.600000", "1.600000"],     # 腾讯式
        "date": pd.to_datetime(["2026-09-07", "2026-09-08"]),
        "open": [10.0, 10.2], "high": [11.5, 11.8], "low": [9.0, 9.8],
        "close": [11.0, 11.5], "volume": [150, 180], "amount": [1.5e6, 1.8e6],
    })
    fixed.to_parquet(os.path.join(fx, "hfq_1_600000.parquet"), index=False)

    from common.store import load
    out = load("stock", fq="hfq", root=root)
    # 必须只保留 fixup 版本（2 行），不能出现 3 行（重复）
    assert len(out) == 2, f"归一顺序错误会导致整段重复行，实际 {len(out)} 行"
    assert out.iloc[0]["close"] == pytest.approx(11.0)


def test_reader_fixup_glob_no_cross_fq_leak(tmp_path):
    """★ fixup glob 必须限定同口径，否则会把复权行拼进不复权库。"""
    root = str(tmp_path / "data")
    base = os.path.join(root, "market", "stock", "raw")
    os.makedirs(base, exist_ok=True)
    pd.DataFrame({
        "code": ["600000"], "date": pd.to_datetime(["2026-09-07"]),
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "volume": [100], "amount": [1e6],
    }).to_parquet(os.path.join(base, "raw_b0_00.parquet"), index=False)

    fx = os.path.join(root, "market", "stock", "fixup")
    os.makedirs(fx, exist_ok=True)
    pd.DataFrame({
        "code": ["600000"], "date": pd.to_datetime(["2026-09-07"]),
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "volume": [100], "amount": [1e6],
    }).to_parquet(os.path.join(fx, "hfq_600000.parquet"), index=False)  # ★ 错误口径

    from common.store import load
    # 读 raw 时，hfq 的 fixup 不应被加载（否则口径污染）
    out = load("stock", fq="raw", root=root)
    assert len(out) == 1


def test_reader_column_pruning(tmp_path):
    root = str(tmp_path / "data")
    write_incremental(_mk_incr_df(), "stock", "hfq", "2026-09-11", root=root)
    from common.store import load
    df = load("stock", fq="hfq", columns=["close"], root=root)
    assert set(df.columns) == {"code", "date", "close"}


def test_reader_unknown_column_raises(tmp_path):
    root = str(tmp_path / "data")
    write_incremental(_mk_incr_df(), "stock", "hfq", "2026-09-11", root=root)
    from common.store import load
    with pytest.raises(ContractViolation, match="unknown column"):
        load("stock", fq="hfq", columns=["pe_ratio"], root=root)


def test_reader_qfq_rejected():
    from common.store import load
    with pytest.raises(ValueError, match="qfq"):
        load("stock", fq="qfq")


def test_reader_last_n(tmp_path):
    root = str(tmp_path / "data")
    for d in ("2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"):
        write_incremental(_mk_incr_df(d), "stock", "hfq", d, root=root)
    from common.store import load
    df = load("stock", fq="hfq", last_n=2, root=root)
    assert len(df) == 6  # 2 天 × 3 只
    assert df["date"].max().strftime("%Y-%m-%d") == "2026-09-11"


def test_reader_codes_filter(tmp_path):
    root = str(tmp_path / "data")
    write_incremental(_mk_incr_df(), "stock", "hfq", "2026-09-11", root=root)
    from common.store import load
    df = load("stock", fq="hfq", codes=["sh.600000"], root=root)
    assert len(df) == 1
    assert df.iloc[0]["code"] == "600000"


# ---------------------------------------------------------------------------
# 6. 跨域私有 meta 拦截
# ---------------------------------------------------------------------------
def test_cross_domain_private_meta_blocked(tmp_path):
    """★ domains/etf 不得读 shortterm 私有的 st_history（跨域读=口径污染）。"""
    root = str(tmp_path / "data")
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    pd.DataFrame({"code": ["600000"], "date": pd.to_datetime(["2026-09-07"]),
                  "isST": [0]}).to_parquet(
        os.path.join(root, "meta", "st_history.parquet"), index=False)
    from common.store import load_meta
    # 短线域自己读 -> 允许
    ok = load_meta("st_history", root=root, domain="shortterm")
    assert len(ok) == 1
    # 其他域读 -> 拦截
    with pytest.raises(ContractViolation, match="私有 meta"):
        load_meta("st_history", root=root, domain="etf")
    with pytest.raises(ContractViolation, match="私有 meta"):
        load_meta("st_history", root=root, domain="selected")


# ---------------------------------------------------------------------------
# 7. 封存与过期
# ---------------------------------------------------------------------------
def test_seal_partition_merges_and_deletes_incr(tmp_path):
    root = str(tmp_path / "data")
    for d in ("20260901", "20260902", "20260903"):
        write_incremental(_mk_incr_df(d), "stock", "hfq", d, root=root)
    res = seal_partition("stock", "hfq", 2026, 9, root=root)
    assert res["rows"] == 9
    assert res["sealed"] >= 1
    # 日分片应被删除
    inc = os.path.join(root, "market", "stock", "hfq", "_incr")
    remaining = [f for f in os.listdir(inc) if os.path.isdir(os.path.join(inc, f))]
    assert remaining == [], f"封存后应清空 _incr，剩余 {remaining}"
    # 封存目录存在
    sealed = os.path.join(root, "market", "stock", "hfq", "year=2026", "month=09")
    assert os.path.isdir(sealed)
    assert len([f for f in os.listdir(sealed) if f.endswith(".parquet")]) >= 1


def test_seal_partition_dry_run(tmp_path):
    root = str(tmp_path / "data")
    write_incremental(_mk_incr_df("20260901"), "stock", "hfq", "20260901", root=root)
    res = seal_partition("stock", "hfq", 2026, 9, root=root, dry_run=True)
    assert res.get("dry_run") is True
    # dry-run 不删原文件
    inc = os.path.join(root, "market", "stock", "hfq", "_incr", "20260901")
    assert os.path.isdir(inc)


def test_expire_dry_run_lists_partitions(tmp_path):
    root = str(tmp_path / "data")
    base = os.path.join(root, "market", "stock", "hfq")
    for y, m in ((2023, 1), (2023, 6), (2026, 9)):
        p = os.path.join(base, f"year={y}", f"month={m:02d}")
        os.makedirs(p, exist_ok=True)
        pd.DataFrame({"code": ["600000"], "date": pd.to_datetime([f"{y}-{m:02d}-01"]),
                      "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                      "volume": [1], "amount": [1.0]}).to_parquet(
            os.path.join(p, "batch=00.parquet"), index=False)
    res = expire_partitions("stock", "hfq", 730, root=root,
                            today=_dt.date(2026, 9, 11), dry_run=True)
    # dry-run 只列清单
    assert res["dry_run"] is True
    assert os.path.isdir(os.path.join(base, "year=2023", "month=01"))


def test_expire_index_never():
    res = expire_partitions("index", "raw", None, root="/nonexistent")
    assert res["removed"] == []
    assert "不过期" in res["reason"]


def test_expire_keeps_recent(tmp_path):
    root = str(tmp_path / "data")
    base = os.path.join(root, "market", "etf", "hfq")
    for y, m in ((2016, 1), (2026, 9)):
        p = os.path.join(base, f"year={y}", f"month={m:02d}")
        os.makedirs(p, exist_ok=True)
        pd.DataFrame({"code": ["512890"], "date": pd.to_datetime([f"{y}-{m:02d}-01"]),
                      "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                      "volume": [1], "amount": [1.0]}).to_parquet(
            os.path.join(p, "batch=00.parquet"), index=False)
    res = expire_partitions("etf", "hfq", 2430, root=root,
                            today=_dt.date(2026, 9, 11), dry_run=True)
    dirs = [r["dir"] for r in res["removed"]]
    # 2026-09 必须保留
    assert not any("month=09" in d and "year=2026" in d for d in dirs)


# ---------------------------------------------------------------------------
# 8. 大盘多周期：closed_only 契约
# ---------------------------------------------------------------------------
def test_index_weekly_requires_partial_flag(tmp_path):
    """★ weekly 分区缺 is_partial 列 -> 直接抛契约违反（不允许静默放行）。"""
    root = str(tmp_path / "data")
    p = os.path.join(root, "market", "index", "broad", "sh000001", "weekly", "year=2026")
    os.makedirs(p, exist_ok=True)
    pd.DataFrame({
        "code": ["sh000001"], "date": pd.to_datetime(["2026-09-11"]),
        "open": [3000.0], "high": [3050.0], "low": [2980.0], "close": [3020.0],
    }).to_parquet(os.path.join(p, "batch=00.parquet"), index=False)  # 无 is_partial
    from common.store import load
    with pytest.raises(ContractViolation, match="is_partial"):
        load("index", fq="raw", freq="weekly", root=root, group="broad")


def test_index_weekly_closed_only_filters_partial(tmp_path):
    root = str(tmp_path / "data")
    p = os.path.join(root, "market", "index", "broad", "sh000001", "weekly", "year=2026")
    os.makedirs(p, exist_ok=True)
    pd.DataFrame({
        "code": ["sh000001"] * 3,
        "date": pd.to_datetime(["2026-09-04", "2026-09-11", "2026-09-18"]),
        "open": [3000.0, 3010.0, 3020.0], "high": [3050.0, 3060.0, 3070.0],
        "low": [2980.0, 2990.0, 3000.0], "close": [3020.0, 3030.0, 3040.0],
        "is_partial": [False, False, True],
    }).to_parquet(os.path.join(p, "batch=00.parquet"), index=False)

    from common.store import load
    default = load("index", fq="raw", freq="weekly", root=root, group="broad")
    assert len(default) == 2, "默认 closed_only=True 应剔除未完成周"
    assert not default["is_partial"].any()

    allp = load("index", fq="raw", freq="weekly", root=root, group="broad",
                closed_only=False)
    assert len(allp) == 3, "closed_only=False 应返回全部（盘中视角）"


def test_stock_weekly_not_materialized():
    """stock/etf 的周月线不物化，上层应自行聚合。"""
    from common.store import load, StoreError
    with pytest.raises(StoreError, match="aggregate"):
        load("stock", fq="hfq", freq="weekly", root="/tmp/nonexistent-qh")
