#!/usr/bin/env python3
"""tools/check_docs.py —— Phase 6 AUTO-KPI 校验（§9.5.2 推广 refresh_docs 机制到合并仓）。

权威数字唯一来源：state/<d>/payload/payload.json（Day-1 转发）或 shadow_payload.json（晋级后）。
本脚本对 README.md 的 `AUTO-KPI:START/END` 锚点做双向维护：
  - 无参运行：用当前 payload KPI 刷新 README 锚点
  - --check-only：只校验，不一致或锚点缺失 → exit 1（CI fail，防"文档数字 vs 产物"分叉）

三份域级 HANDOFF（docs/handoff/*.md）原样保留（§9.5 不重写），若其中存在同款锚点也一并校验。

用法:
  .venv/bin/python tools/check_docs.py                 # 刷新 README.md 锚点
  .venv/bin/python tools/check_docs.py --check-only    # CI 校验（不一致 exit 1）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import shadow_diff  # noqa: E402

ANNO_START = "<!-- AUTO-KPI:START"
ANNO_END = "<!-- AUTO-KPI:END -->"

# 各域 KPI 的文档化名称（与 shadow_diff.extract_kpis 顺序严格对应）
_DOC_NAMES = {
    "shortterm": ["动量开·收益", "动量开·回撤", "动量开·夏普", "动量开·笔数", "动量开·胜率", "动量开·最后权益",
                  "动量关·收益", "动量关·回撤", "动量关·夏普", "动量关·笔数", "动量关·胜率", "动量关·最后权益",
                  "黑盒开·收益", "黑盒开·回撤", "黑盒开·夏普", "黑盒开·笔数", "黑盒开·胜率", "黑盒开·最后权益",
                  "黑盒关·收益", "黑盒关·回撤", "黑盒关·夏普", "黑盒关·笔数", "黑盒关·胜率", "黑盒关·最后权益"],
    "etf": ["红利低波·总收益", "红利低波·年化", "红利低波·夏普", "红利低波·回撤", "红利低波·笔数",
            "红利低波·基准收益", "红利低波·基准夏普", "红利低波·基准回撤",
            "行业轮动·总收益", "行业轮动·年化", "行业轮动·夏普", "行业轮动·回撤", "行业轮动·笔数",
            "沪深300·总收益", "沪深300·年化", "沪深300·夏普", "沪深300·回撤", "沪深300·笔数"],
    "selected": [],  # 无基线（factors 为空）→ 不生成 KPI 行
}

_DOMAIN_LABEL = {"shortterm": "短线策略", "etf": "ETF策略", "selected": "个性化选股"}

# 逻辑域 -> 统一信封里的真实 domain 标识（state/payload/{domain}.{variant}.json）
_DOMAIN_MAP = {"shortterm": "quant-lab", "etf": "etf", "selected": "stock"}


def _load_payloads(domain: str) -> list[dict]:
    """读该域在 state/payload/ 下的全部统一信封（carry-forward 后必有上次成功版本）。"""
    d = ROOT / "state" / "payload"
    if not d.exists():
        return []
    pat = f"{_DOMAIN_MAP.get(domain, domain)}.*.json"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob(pat))]


def _fmt(v: float, kind: str) -> str:
    # ETF metrics.total 等是"倍数"（2.87 = +286.5%），此处统一按小数比格式化
    if kind in ("总收益", "年化", "收益"):
        return f"{v:+.2%}"
    if kind in ("回撤", "胜率", "基准收益", "基准回撤"):
        return f"{v:.2%}"
    if kind in ("夏普", "基准夏普", "年化夏普"):
        return f"{v:.2f}"
    if "笔数" in kind:
        return f"{int(v)}"
    if "最后权益" in kind:
        return f"¥{v:,.0f}"
    return f"{v:.2f}"


def _kpi_round_trip(domain: str) -> list[tuple[str, float]]:
    """KPI 名 + 原始值（供文档渲染与比对）。"""
    payloads = _load_payloads(domain)
    if not payloads:
        return []
    kpis = shadow_diff.extract_kpis(domain, payloads)
    names = _DOC_NAMES[domain]
    return list(zip(names, kpis))


def _render_block() -> str:
    lines = [f"{ANNO_START} (check_docs.py 生成, 严禁手改; 权威数字来自 state/<d>/payload) -->", ""]
    for domain in ("shortterm", "etf", "selected"):
        rows = _kpi_round_trip(domain)
        label = _DOMAIN_LABEL[domain]
        if not rows:
            lines.append(f"**{label}**：无 KPI（payload 缺失或该域无基线）。")
            lines.append("")
            continue
        lines.append(f"**{label} KPI**：")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        for name, v in rows:
            kind = name.split("·")[-1]
            lines.append(f"| {name} | {_fmt(v, kind)} |")
        lines.append("")
    lines.append(f"> 由 `tools/check_docs.py` 生成；`--check-only` 在 CI 中校验与 payload 一致，严禁手改。")
    lines.append(ANNO_END)
    return "\n".join(lines)


def _update_anchor(text: str, block: str) -> str:
    if ANNO_START not in text or ANNO_END not in text:
        raise SystemExit(f"缺少 {ANNO_START}...{ANNO_END} 锚点 —— 请先插入占位块")
    import re
    return re.sub(rf"{re.escape(ANNO_START)}.*?{re.escape(ANNO_END)}",
                  lambda m: block.replace("\\", "\\\\"), text, flags=re.S)


def _check_anchor(text: str, path: Path, errors: list[str]) -> None:
    if ANNO_START not in text or ANNO_END not in text:
        errors.append(f"{path.name}: 缺少 AUTO-KPI 锚点（防锚点被删后静默失效）")
        return
    expected = _render_block()
    expected_norm = "\n".join(line.strip() for line in expected.splitlines() if line.strip())
    actual_norm = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    # 仅比对锚点区间
    import re
    m = re.search(rf"{re.escape(ANNO_START)}.*?{re.escape(ANNO_END)}", text, re.S)
    if not m:
        errors.append(f"{path.name}: AUTO-KPI 锚点区间无法匹配")
        return
    actual_block = m.group(0)
    a_norm = "\n".join(l.strip() for l in actual_block.splitlines() if l.strip())
    e_norm = "\n".join(l.strip() for l in expected.splitlines() if l.strip())
    if a_norm != e_norm:
        errors.append(f"{path.name}: AUTO-KPI 数字与 payload 不一致（跑 tools/check_docs.py 刷新）")


def main() -> None:
    check_only = "--check-only" in sys.argv
    readme = ROOT / "README.md"
    if not readme.exists():
        raise SystemExit(f"缺少 {readme} —— README 是 AUTO-KPI 锚点宿主，先创建")

    text = readme.read_text(encoding="utf-8")
    block = _render_block()

    if not check_only:
        readme.write_text(_update_anchor(text, block), encoding="utf-8")
        print("[check_docs] README.md 锚点已刷新")
        return

    errors: list[str] = []
    _check_anchor(text, readme, errors)
    # 三份域级 HANDOFF（docs/handoff/*.md）原样保留（§9.5），其中锚点由各老仓
    # 自己的 refresh_docs/CI 守护；合并仓不覆盖、不校验其数字（防误伤冻结基线）。

    for e in errors:
        print(f"::error:: {e}")
    if errors:
        raise SystemExit(f"[check_docs] {len(errors)} 处不一致 → 跑 tools/check_docs.py 刷新后提交")
    print("[check_docs] AUTO-KPI 校验通过：README 数字与 payload 一致")


if __name__ == "__main__":
    main()
