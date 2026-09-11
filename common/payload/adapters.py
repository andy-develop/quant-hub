"""三域 payload 适配器（方案 §6）。

## 为什么需要这一层

三域的产物格式各不一样，且**都在各自的构建脚本里硬编码了域名、id 前缀、
输出路径**。合并后前端是一个单页壳，同一份数据要同时服务：
  - 独立单域页面（保留，便于单独部署/回滚）
  - 合并后的单页壳

做法：**适配器只做「读原生产物 -> 规范化为统一 envelope」**，不改各域脚本。
构建期的注入由 `web.build` 负责，运行期的数据不落第二份。

## 统一 envelope

```json
{
  "domain": "quant-lab | etf | stock",
  "variant": "momentum | blackbox | dividend | sector | hs300 | screen",
  "generated_at": "2026-09-11T20:32:11+08:00",
  "data_date": "2026-09-11",
  "schema_version": 1,
  "payload": { ... 原生数据，原样保留 ... }
}
```

`variant` 是**关键**：短线域有两条独立策略线（动量 + 量化黑盒），
方案原文只列了 12 个路由，漏掉了黑盒线 —— 实测 `lgbm_rank.py` 于 09-10
上线后 `build_report.py` 会产出**两份** payload（`__DATA__` 与 `__DATA_BB__`），
路由应为 13 条。详见 `差异核对报告.md`。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DomainPayload",
    "SCHEMA_VERSION",
    "normalize_quant_lab",
    "normalize_etf",
    "normalize_stock",
    "ADAPTERS",
    "ROUTES",
    "route_table",
    "envelope",
]

SCHEMA_VERSION = 1
TZ_CST = _dt.timezone(_dt.timedelta(hours=8))


def _now_iso() -> str:
    return _dt.datetime.now(TZ_CST).isoformat(timespec="seconds")


def envelope(domain: str, variant: str, payload: Any, *,
             data_date: str | None = None,
             generated_at: str | None = None,
             extra: dict | None = None) -> dict:
    """统一的 payload 信封。"""
    out = {
        "domain": domain,
        "variant": variant,
        "generated_at": generated_at or _now_iso(),
        "data_date": data_date,
        "schema_version": SCHEMA_VERSION,
        "payload": payload,
    }
    if extra:
        out.update(extra)
    return out


@dataclass
class DomainPayload:
    domain: str
    variant: str
    payload: Any
    data_date: str | None = None
    generated_at: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_envelope(self) -> dict:
        return envelope(self.domain, self.variant, self.payload,
                        data_date=self.data_date,
                        generated_at=self.generated_at,
                        extra={"warnings": self.warnings} if self.warnings else None)


# ---------------------------------------------------------------------------
# 域 1：短线策略（quant-lab）
# ---------------------------------------------------------------------------
def normalize_quant_lab(*, modes: dict | None = None,
                        modes_bb: dict | None = None,
                        data_date: str | None = None) -> list[DomainPayload]:
    """量化短线域 -> 两条 payload。

    ★ 方案漏列黑盒线：实测 `build_report.py` 的 `render_html(modes, modes_bb)`
      会注入两份数据（`__DATA__` / `__DATA_BB__`）。方案原文只按单 payload
      设计路由表，漏掉后黑盒页在合并站点里会**永远空白**。

    输入形如：
        modes    = {"y3": {"on": {...}, "off": {...}}}
        modes_bb = {"y3": {"on": {...}, "off": {...}}}
    """
    out: list[DomainPayload] = []
    warns: list[str] = []

    if not modes:
        warns.append("modes 为空 —— 动量线无数据")
    if not modes_bb:
        warns.append("modes_bb 为空 —— 量化黑盒线无数据"
                     "（lgbm_rank.py 未运行或产物缺失）")

    out.append(DomainPayload("quant-lab", "momentum", modes or {},
                             data_date=data_date, warnings=list(warns)))
    out.append(DomainPayload("quant-lab", "blackbox", modes_bb or {},
                             data_date=data_date, warnings=list(warns)))
    return out


def extract_quant_lab_html(report_py_path: str) -> tuple[dict, dict]:
    """从已生成的 report/index.html 里把两份 payload 抠出来。

    用于「直接消费构建产物」的路径（构建脚本不重跑策略，只重组页面）。
    """
    import re

    with open(report_py_path, encoding="utf-8") as f:
        src = f.read()
    # 兼容直接读 index.html 与读 build_report.py 两种输入
    text = src
    m1 = re.search(r"const\s+MODES\s*=\s*(\{.*?\});", text, re.S)
    m2 = re.search(r"const\s+MODES_BB\s*=\s*(\{.*?\});", text, re.S)
    modes = json.loads(m1.group(1)) if m1 else {}
    modes_bb = json.loads(m2.group(1)) if m2 else {}
    return modes, modes_bb


# ---------------------------------------------------------------------------
# 域 2：ETF 策略（red-dividend-strategy）
# ---------------------------------------------------------------------------
def normalize_etf(payload: dict | None = None, *,
                  data_date: str | None = None) -> list[DomainPayload]:
    """ETF 域 -> 三条 payload。

    原生 payload 结构（`update.py:build_backtest_payload`）：
        {"snapshot": ..., "backtest": ..., "sector": ..., "hs300": ...}

    其中 `sector`（行业轮动）与 `hs300`（沪深300 择时）由**各自的脚本**
    写入同一 PAYLOAD 块，采用 carry-forward 机制互不覆盖 —— 适配器要把它们
    拆成独立 variant，否则合并站点里没法单独路由/单独判新鲜度。
    """
    payload = payload or {}
    out: list[DomainPayload] = []

    main = {k: v for k, v in payload.items()
            if k in ("snapshot", "backtest")}
    out.append(DomainPayload("etf", "dividend", main, data_date=data_date))

    if payload.get("sector") is not None:
        out.append(DomainPayload("etf", "sector", payload["sector"],
                                 data_date=data_date))
    if payload.get("hs300") is not None:
        out.append(DomainPayload("etf", "hs300", payload["hs300"],
                                 data_date=data_date))
    return out


# ---------------------------------------------------------------------------
# 域 3：个性化选股（stock-factor-engine）
# ---------------------------------------------------------------------------
def normalize_stock(*, stocks: list | None = None,
                    factors: dict | None = None,
                    data_date: str | None = None,
                    generated_at: str | None = None) -> list[DomainPayload]:
    """个性化选股域 -> 一条 payload。

    原生结构（`build_html.py`）：
        stocks  : `[["000001","平安银行"], ...]` —— 5180 只，紧凑二元组
        factors : `{"000001": {...}, ...}` —— 实测当前为 `{}`（空）

    ★ 两个坑：
      1. `stocks.json` 是**列表**不是 dict；元素是 `[code, name]` 二元组
      2. 前端模板用 `REAL_FACTORS[code]` 取值，key 必须与 stocks 里的 code
         **同格式**（都是 6 位裸码），不能用 secid
    """
    warns: list[str] = []
    stocks = stocks or []
    factors = factors or {}

    if not stocks:
        warns.append("stocks.json 为空 —— 股票池为空，页面将无候选")
    if not factors:
        warns.append("factors.json 为空 —— 因子数据缺失，评分/相似度全部退化为占位值")
    bad = [s for s in stocks if not (isinstance(s, (list, tuple)) and len(s) >= 2)]
    if bad:
        warns.append(f"{len(bad)} 条股票记录格式异常（应为 [code, name]）")

    return [DomainPayload(
        "stock", "screen",
        {"stocks": stocks, "factors": factors},
        data_date=data_date, generated_at=generated_at, warnings=warns)]


# ---------------------------------------------------------------------------
# 路由表（★ 13 条，含方案漏掉的黑盒线）
# ---------------------------------------------------------------------------
ADAPTERS = {
    "quant-lab": normalize_quant_lab,
    "etf": normalize_etf,
    "stock": normalize_stock,
}

# (domain, variant, 页面标题)
ROUTES: list[tuple[str, str, str]] = [
    ("quant-lab", "momentum", "动量策略"),
    ("quant-lab", "blackbox", "量化黑盒"),     # ★ 方案漏列
    ("etf", "dividend", "红利低波"),
    ("etf", "sector", "行业轮动"),
    ("etf", "hs300", "沪深300 择时"),
    ("stock", "screen", "因子选股"),
]


def route_table() -> list[dict]:
    return [{"domain": d, "variant": v, "title": t} for d, v, t in ROUTES]


def slug(domain: str, variant: str) -> str:
    return f"{domain}.{variant}"


def write_envelopes(payloads: list[DomainPayload], out_dir: str) -> list[str]:
    """落盘统一信封（`state/payload/{domain}.{variant}.json`）。"""
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for p in payloads:
        path = os.path.join(out_dir, f"{slug(p.domain, p.variant)}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(p.to_envelope(), f, ensure_ascii=False, indent=1)
        paths.append(path)
    return paths
