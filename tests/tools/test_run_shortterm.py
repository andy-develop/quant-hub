#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_shortterm._extract_modes 回归：从 build_report 产物 HTML 抽 MODES/MODES_BB。"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from tools.run_shortterm import _extract_modes

HTML = '''<html><script>
const MODES = {"y3": {"on": {"dates": ["2024-01-02", "2026-09-10"], "equity": [1000000, 2141000], "kpi": {"ret": 1.141}}}};
const MODES_BB = {"y3": {"on": {"dates": ["2024-01-02"], "kpi": {"ret": 0.065}}}};
initPage('', MODES, 'x');
</script></html>'''

def test_extract_modes_and_bb():
    modes, bb = _extract_modes(HTML)
    assert modes["y3"]["on"]["kpi"]["ret"] == 1.141
    assert modes["y3"]["on"]["dates"][-1] == "2026-09-10"
    assert bb["y3"]["on"]["kpi"]["ret"] == 0.065

def test_extract_handles_nested_braces():
    # JSON 内含多层嵌套花括号也能正确抽到（非贪婪到下一个 const）
    h = 'const MODES = {"a": {"b": {"c": 1}}, "d": [1,2,3]};\nconst MODES_BB = {"x": 1};\n'
    modes, bb = _extract_modes(h)
    assert modes["a"]["b"]["c"] == 1 and modes["d"] == [1, 2, 3]
    assert bb["x"] == 1
