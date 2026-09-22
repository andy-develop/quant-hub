"""短线域「双页」装配回归（2026-09 线上缺陷）。

背景：`web/build.py` 之前只抽内层 `HTML_TEMPLATE`，没跑域内 `render_html()`
的两步——(1) 克隆出「量化黑盒」页、(2) 替换 `__STRAT_DOC__` / `__ST_BANNER__` /
`__MAX_HOLD__`。后果是线上合并页：顶部挂着字面量 `__ST_BANNER__`、「策略说明」
卡里是 `__STRAT_DOC__`、页脚 `持有满__MAX_HOLD__日退出`，侧栏「量化黑盒」点进去
空白，且 `initPage('bb_')` 因缺 `bb_rangeTabs` 抛 TypeError 中断整段初始化。

这里锁死的是「合并层必须独立完成这两步」——复用域内 render_html 不行（它依赖
pandas/numpy，而合并层被要求仅用标准库，见 ci.yml）。
"""
from __future__ import annotations

import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_REPO_ROOT, "domains")
_BUILD_REPORT = os.path.join(_SRC, "shortterm/scripts/build_report.py")
_PAYLOAD = os.path.join(_REPO_ROOT, "state/payload")

_PLACEHOLDERS = ("__STRAT_DOC__", "__ST_BANNER__", "__MAX_HOLD__",
                 "__KPI_BLOCK__", "__DATA__", "__DATA_BB__")


def _source() -> str:
    if not os.path.exists(_BUILD_REPORT):
        pytest.fail("模板源不可用：domains/shortterm/scripts/build_report.py 不存在")
    with open(_BUILD_REPORT, encoding="utf-8") as f:
        return f.read()


def _assemble(envelopes: dict | None = None) -> str:
    from web.build import _assemble_quant_lab, inject_payloads

    env = envelopes or {}
    return inject_payloads(_assemble_quant_lab(_source(), _SRC, env), "quant-lab", env)


def test_two_pages_and_no_placeholder_left():
    html = _assemble()
    assert 'id="page-momentum"' in html
    assert 'id="page-blackbox" style="display:none"' in html, "黑盒页没克隆出来 -> 侧栏死链"
    for ph in _PLACEHOLDERS:
        assert ph not in html, f"占位符未替换（会当字面量显示）: {ph}"


def test_blackbox_page_has_its_own_dom():
    """黑盒页每个 id 都要有 bb_ 版本，否则 initPage('bb_') 找不到节点即抛错。"""
    html = _assemble()
    for el in ("bb_rangeTabs", "bb_modeSw", "bb_eqChart", "bb_reasonChart",
               "bb_tradeToggle", "bb_warnBar", "bb_foot", "bb_sub"):
        assert f'id="{el}"' in html, f"缺 {el}"


def test_hold_limit_comes_from_engine_not_hardcoded():
    """持有上限必须来自 engine.MAX_HOLD（域内也是这么取的，别又硬编码撒谎）。"""
    from web.build import _max_hold

    n = _max_hold(_SRC)
    assert n.isdigit()
    html = _assemble()
    assert f"拿满 {n} 个交易日" in html        # 策略说明·到期卖出
    assert f"持有满{n}日退出" in html           # 页脚回测口径


def test_two_pages_carry_their_own_strategy_doc():
    """动量页说加权排队、黑盒页说 LightGBM —— 文案不能两页一份。"""
    html = _assemble()
    assert "机器学习排序模型（LightGBM）" in html, "黑盒版说明没接上"
    # 黑盒页不得残留动量版的「打分排队」段（说明 _P_MOM -> _P_BB 替换生效）
    bb_doc = html.split('id="page-blackbox"', 1)[1]
    assert "机器学习排序模型（LightGBM）" in bb_doc
    assert "打分排队，六项指标加权" not in bb_doc


def test_kpi_block_reflects_payload():
    """给了 payload 就用真实数字算，且两页各算各的。"""
    import json

    if not os.path.isdir(_PAYLOAD):
        pytest.fail("缺少 state/payload —— 这条测试实际上没跑")
    env = {}
    for name in os.listdir(_PAYLOAD):
        if name.startswith("quant-lab."):
            with open(os.path.join(_PAYLOAD, name), encoding="utf-8") as f:
                o = json.load(f)
            env[(o["domain"], o["variant"])] = o
    html = _assemble(env)
    assert "暂无回测数据" not in html
    assert "+125.5%" in html  # 动量 · 开（当前 payload）
    # 黑盒页的 KPI 是自己的（与动量不同）
    mom_doc = html.split('id="page-blackbox"', 1)[0]
    bb_doc = html.split('id="page-blackbox"', 1)[1]
    assert "+125.5%" in mom_doc
    assert "+125.5%" not in bb_doc, "黑盒页用了动量页的 KPI"


def test_scoped_pages_have_no_duplicate_or_dangling_ids():
    """克隆后 id 全局唯一，且每个 el()/getElementById() 引用都指向存在的节点。"""
    from web.build import duplicate_ids
    from web.shell.scope import scope_html_fragment

    html = _assemble()
    dup = duplicate_ids({"quant-lab": html})
    assert dup == set(), f"黑盒页克隆引入跨域重名 id: {dup}"
    out = scope_html_fragment(html, "quant-lab", wrap=False, rename=dup)
    ids = re.findall(r'\bid="([\w-]+)"', out)
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    assert not repeated, f"作用域化后仍有重名 id: {repeated}"
    have = set(ids)
    ref = re.compile(r'(?:getElementById|(?<![\w$.])\$|(?<![\w$.])el)'
                     r'\(\s*["\']([\w-]+)["\']\s*\)')
    missing = sorted({m for m in ref.findall(out) if m not in have})
    assert not missing, f"引用了不存在的 id（脚本会中断）: {missing}"


def test_full_build_is_clean(tmp_path):
    """端到端：真实构建产物不得含占位符字面量，且黑盒页在。"""
    from web.build import build

    payload_dir = _PAYLOAD if os.path.isdir(_PAYLOAD) else None
    html = build(_SRC, str(tmp_path / "index.html"), echarts_inline=False,
                 payload_dir=payload_dir)
    for ph in _PLACEHOLDERS:
        assert ph not in html, f"产物残留占位符: {ph}"
    assert 'id="bb_rangeTabs"' in html and 'id="page-blackbox"' in html