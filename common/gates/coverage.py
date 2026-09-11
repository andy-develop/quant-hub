"""覆盖率硬门禁（方案 §0.4 / §9.9）。

这条不是"以防万一"—— §0.4 已经实测出短线域连续 4 个交易日丢失整个沪市
（覆盖率 1.7%）而 CI 全绿、`verify_store` 全 PASS。

规则（三域与三个数据 workflow 共用）：

    当日入库标的 / 在市标的
      < 95%  -> 黄（发布但状态条标黄）
      < 80%  -> 红、中止、不落盘（宁缺勿错，21:00 补跑 cron 会补齐）

分市场覆盖与整体覆盖都查 —— §0.4 的故障正是"整体 55%、沪市 1.7%"，
只看整体会漏。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

__all__ = [
    "CoverageResult",
    "check_coverage",
    "check_stock_coverage",
    "CoverageGateError",
    "GREEN",
    "YELLOW",
    "RED",
]

GREEN, YELLOW, RED = "green", "yellow", "red"

# 阈值（方案 §0.4 建议处理 3 / §2.5 L4）
WARN_THRESHOLD = 0.95
FAIL_THRESHOLD = 0.80


class CoverageGateError(AssertionError):
    """覆盖率红：中止且不落盘。"""


@dataclass
class CoverageResult:
    asset: str
    day: str
    level: str = GREEN
    expected: int = 0
    got: int = 0
    overall: float = 0.0
    by_market: dict[str, float] = field(default_factory=dict)
    missing_sample: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def ok_to_write(self) -> bool:
        return self.level != RED

    def to_dict(self) -> dict:
        return {
            "asset": self.asset, "day": self.day, "level": self.level,
            "expected": self.expected, "got": self.got,
            "overall": round(self.overall, 4),
            "by_market": {k: round(v, 4) for k, v in self.by_market.items()},
            "missing_sample": self.missing_sample[:20],
            "reasons": self.reasons,
        }

    def summary(self) -> str:
        mm = " ".join(f"{k}={v:.1%}" for k, v in sorted(self.by_market.items()))
        return (f"[coverage/{self.asset}] {self.day} {self.level.upper()} "
                f"{self.got}/{self.expected} = {self.overall:.1%} ({mm})")


def _market_of(code: str) -> str:
    """短代码 -> 市场标识（沪/深/北）。"""
    c = str(code).strip()
    if c.upper().startswith("H") or not c.isdigit():
        return "other"
    if c[0] in ("6", "9"):
        return "sh"
    if c[0] in ("0", "2", "3"):
        return "sz"
    if c[0] in ("4", "8"):
        return "bj"
    return "other"


def check_coverage(
    got_codes: Iterable[str],
    expected_codes: Iterable[str],
    *,
    asset: str,
    day: str,
    warn: float = WARN_THRESHOLD,
    fail: float = FAIL_THRESHOLD,
    raise_on_red: bool = True,
    per_market_floor: bool = True,
) -> CoverageResult:
    """计算并判定覆盖率。

    参数
    ----
    got_codes      : 当日实际入库的代码集合
    expected_codes : 在市（可交易）标的全集，来自 meta/universe
    per_market_floor: 是否额外按市场分别设下限（★ §0.4 的关键：
                      整体 55% 但沪市 1.7%，只看整体会漏掉整市场缺失）
    raise_on_red   : 红时是否抛异常（默认抛，调用方据此中止且不落盘）
    """
    got = {str(c).strip() for c in got_codes if str(c).strip()}
    exp = {str(c).strip() for c in expected_codes if str(c).strip()}
    res = CoverageResult(asset=asset, day=day, expected=len(exp), got=len(got & exp))

    if not exp:
        res.level = RED
        res.reasons.append("expected universe is empty —— 无法判定覆盖率（怀疑 universe 未加载）")
        if raise_on_red:
            raise CoverageGateError(res.summary() + " | " + "; ".join(res.reasons))
        return res

    res.overall = res.got / len(exp)

    # 分市场统计
    exp_by_mkt: dict[str, set[str]] = {}
    for c in exp:
        exp_by_mkt.setdefault(_market_of(c), set()).add(c)
    got_set = got & exp
    for mkt, cs in exp_by_mkt.items():
        hit = len(cs & got_set)
        res.by_market[mkt] = hit / len(cs) if cs else 0.0

    # 缺失样本（便于排查，最多留 20 个）
    missing = sorted(exp - got_set)
    res.missing_sample = missing[:20]

    # 分级
    worst = min(res.by_market.values()) if (per_market_floor and res.by_market) else res.overall
    if res.overall < fail or (per_market_floor and worst < fail):
        res.level = RED
    elif res.overall < warn or (per_market_floor and worst < warn):
        res.level = YELLOW
    else:
        res.level = GREEN

    if res.level == RED:
        bad = [f"{m}={v:.1%}" for m, v in sorted(res.by_market.items()) if v < fail]
        res.reasons.append(
            f"覆盖率红：整体 {res.overall:.1%}，市场最低 {worst:.1%}"
            + (f"（{', '.join(bad)}）" if bad else "")
            + f"；阈值 fail={fail:.0%}。宁缺勿错，中止且不落盘（21:00 补跑会补齐）"
        )
        if len(missing) > 20:
            res.reasons.append(f"缺失 {len(missing)} 只，示例 {missing[:10]}")
        if raise_on_red:
            raise CoverageGateError(res.summary() + " | " + "; ".join(res.reasons))
    elif res.level == YELLOW:
        soft = [f"{m}={v:.1%}" for m, v in sorted(res.by_market.items()) if v < warn]
        res.reasons.append(
            f"覆盖率黄：整体 {res.overall:.1%}"
            + (f"（{', '.join(soft)}）" if soft else "")
            + f"；阈值 warn={warn:.0%}。发布但状态条标黄"
        )
    return res


def check_stock_coverage(
    got_codes: Iterable[str],
    universe,
    *,
    day: str,
    raise_on_red: bool = True,
    tradable_only: bool = True,
) -> CoverageResult:
    """个股专用入口：从 universe（DataFrame 或代码列表）取在市标的。

    universe 支持：
    - pandas DataFrame，含 `code` 与（可选）`status` / `listed` 列
    - 可迭代的代码

    §0.4 实测基线：正常日沪市 99.4% / 深市 95.9%；
                  故障日沪市 **1.7%** / 深市 99.6%。
    """
    codes = _universe_codes(universe, tradable_only=tradable_only)
    return check_coverage(got_codes, codes, asset="stock", day=day,
                          raise_on_red=raise_on_red)


def _universe_codes(universe, *, tradable_only: bool = True) -> list[str]:
    # pandas DataFrame 路径
    if hasattr(universe, "columns") and hasattr(universe, "to_dict"):
        cols = set(universe.columns)
        df = universe
        if "code" not in cols:
            raise ValueError("universe DataFrame must have a 'code' column")
        if tradable_only and "status" in cols:
            # status=='1' 表示在市（quant-lab 的 stock_basic 口径）
            try:
                df = df[df["status"].astype(str).isin(("1", "True", "true", "listed"))]
            except Exception:
                pass
        return [str(c).strip() for c in df["code"].tolist() if str(c).strip()]
    return [str(c).strip() for c in universe if str(c).strip()]


# ---------------------------------------------------------------------------
# 覆盖率门禁的 CI 入口
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    """CLI：从 manifest 与其同日 universe 计算覆盖率并决定是否允许落盘。

    用法：
        python -m common.gates.coverage --asset stock --day 2026-09-11 \
            --data-root data --write-runlog state/data/runlog
    """
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", required=True)
    ap.add_argument("--day", required=True)
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--write-runlog", default=None)
    ap.add_argument("--no-raise", action="store_true")
    args = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))

    got = _collect_got(args.data_root, args.asset, args.day)
    exp = _collect_expected(args.data_root, args.asset)

    res = check_coverage(got, exp, asset=args.asset, day=args.day,
                         raise_on_red=not args.no_raise)
    print(res.summary())
    for r in res.reasons:
        print("  -", r)

    if args.write_runlog:
        _write_runlog(args.write_runlog, args.day, res)

    return 0 if res.ok_to_write else 1


def _collect_got(root: str, asset: str, day: str) -> set[str]:
    """从当日 _incr 分片里读实际入库代码。"""
    import glob
    import os

    d = day.replace("-", "")
    out: set[str] = set()
    for fq in ("raw", "hfq"):
        for p in glob.glob(os.path.join(root, "market", asset, fq, "_incr", d, "*.parquet")):
            try:
                import pandas as pd

                col = pd.read_parquet(p, columns=["code"])
                out |= {str(c).strip() for c in col["code"].tolist()}
            except Exception:
                continue
    return out


def _collect_expected(root: str, asset: str) -> set[str]:
    import os

    p = os.path.join(root, "meta", "universe.parquet")
    if os.path.exists(p):
        try:
            import pandas as pd

            return set(_universe_codes(pd.read_parquet(p)))
        except Exception:
            pass
    # 回退：从当日分片的历史并集推断（弱化，仅用于无 universe 的过渡期）
    import glob

    out: set[str] = set()
    for fq in ("raw", "hfq"):
        for p in glob.glob(os.path.join(root, "market", asset, fq, "**", "*.parquet"),
                           recursive=True):
            try:
                import pandas as pd

                out |= {str(c).strip() for c in
                        pd.read_parquet(p, columns=["code"])["code"].tolist()}
            except Exception:
                continue
    return out


def _write_runlog(dirpath: str, day: str, res: CoverageResult) -> str:
    import os

    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, f"{day}.coverage.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(res.to_dict(), f, ensure_ascii=False, indent=2)
    return p


if __name__ == "__main__":
    raise SystemExit(main())
