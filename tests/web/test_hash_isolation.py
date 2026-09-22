"""hash 归属域（2026-09-22 三补）。

三域模板各自把 `location.hash` 当自己的路由（etf `#/timing-hs300`、stock `#/factors`），
合并后共用一条地址栏，而三家的 `hashchange` 监听器都在听同一个事件。改前实测：

  · 切域后按返回键 → hash 是上一个域的路由，当前域的 handler 认不出、另一个域的
    handler 又没在看 → 页面纹丝不动、地址栏却是旧的（「返回键失灵」）；
  · 两家路由名撞车时，还会在**看不见的域**里偷偷把视图切了（改状态、不报错）。

修法（域模板一个字没改，全在壳层）：域脚本注册 `hashchange` 时打上自己的门牌
（`__QH_DOMAIN`，由 `_domain_section` 在域内容之前注入），只有「自己正在看」且
「这条 hash 不是别人写的」才响应；每域记住自己最后那条 hash；壳层兜底把从返回键/
深链进来的别域 hash 切过去。

★ 两个踩过的坑，用例点名锁住：
  1. 空 hash **不能认领** —— 认领了之后，壳层兜底会把「没有 hash」当成「上个写手的地盘」，
     一切到不带 hash 的域就被拨回去（实测：Home+回车切不动）。
  2. 壳层自己写下的 hash 要**当场记归属** —— 不记的话紧接着补的那次 hashchange 会被
     当成外来路由，两域来回弹到爆栈（实测：返回键连按两次 `Maximum call stack`）。
"""
from __future__ import annotations

import re

import pytest

from web.build import DOMAINS, SHELL_BOOT_JS, SHELL_JS, _assemble, _domain_section


def _shell(default: str = "quant-lab") -> str:
    body = [_domain_section(k, f'<script>/* {k} */</script>') for k in DOMAINS]
    return _assemble(body=body, head_assets="", health={}, default_domain=default)


def test_boot_patch_is_in_the_head_before_every_domain():
    """★ 门牌 patch 必须**跑在所有域脚本之前**：那些 `addEventListener("hashchange")`
    是在域脚本里同步调用的，放到页尾再 patch 就已经晚了。"""
    html = _shell()
    boot = html.index("window.__QH =")
    assert boot < html.index("<body"), "boot 脚本不在 head 里"
    for k in DOMAINS:
        assert boot < html.index(f'window.__QH_DOMAIN="{k}"'), f"{k} 的门牌比 patch 还早"


def test_every_domain_gets_exactly_one_owner_marker():
    html = _shell()
    for k in DOMAINS:
        assert html.count(f'window.__QH_DOMAIN="{k}";') == 1, f"{k} 的门牌不是恰好一块"


def test_patch_gates_by_active_domain_and_foreign_owner():
    js = SHELL_BOOT_JS
    assert 'type !== "hashchange"' in js, "没按事件类型分流（别的监听器要原样放行）"
    assert "QH.active !== own" in js, "没按「我看没在看」这个域过滤"
    assert "QH.owner[h] !== own" in js, "没挡住别人写的 hash"


def test_empty_hash_is_never_claimed():
    """★ 空 hash 认领了 → 壳层会把「没有 hash」当外来路由，切域被拨回去。"""
    js = SHELL_BOOT_JS
    assert re.search(r"if \(h\) QH\.owner\[h\] = own;", js), \
        "认领没有限定非空 —— 空 hash 会被记成某个域的地盘"


def test_shell_listener_bypasses_the_patch():
    """★ 壳层自己的监听器不属于任何一域；走 patch 会被门牌挡住（那时 `__QH_DOMAIN`
    停在最后一个域，等于把整条兜底逻辑归档给了它）。"""
    assert "QH.onHash = function(fn){ add(\"hashchange\", fn); }" in SHELL_BOOT_JS
    assert "QH.onHash(function(){" in SHELL_JS


def _strip_comments(js: str) -> str:
    """去掉注释再断言 —— 注释里会写反例（“不要 `location.hash = x`”）。"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


def test_shell_normalises_history_without_polluting_it():
    """★ 用户自己切域 = 一次真导航（pushState，返回键才退得回去）；
    壳层响应返回键 = replaceState（再 push 就永远退不出去）。"""
    js = SHELL_JS
    assert 'mode === "replace" ? "replaceState" : "pushState"' in js
    assert 'show(d, "replace")' in js, "壳层兜底没走 replace"
    code = _strip_comments(js)
    # 读 hash 没问题；**写** hash 一律走 history API（语义明确、才控得住历史条目）
    assert not re.search(r"location\.hash\s*=[^=]", code), \
        "还在直接写 location.hash（与 pushState 混着来，返回键会乱）"


def test_ownership_is_recorded_when_the_shell_writes_a_hash():
    """★ 坑 2：写下去的 hash 当场记归属，否则补发的 hashchange 被当外来路由 → 爆栈。"""
    js = SHELL_JS
    assert "if (target) QH.owner[target] = dom;" in js


def test_memory_only_records_hashes_that_belong_to_that_domain():
    """★ 离开一域时地址栏里可能正躺着**上一域**的 hash（壳层刚响应完返回键、还没归位），
    照抄下来就把两域的记忆串了（实测：返回键第二次就不对、并开始乒乓）。"""
    js = SHELL_JS
    assert "QH.owner[cur] === QH.active" in js, "记录 memory 时没验归属"


def test_missing_memory_falls_back_to_the_domain_marker():
    """★ 新域（还没导航过）写 `#<域>` 而不是清空：刷新/分享才不丢「我在哪一域」。"""
    js = SHELL_JS
    assert 'var url = target || "#" + dom;' in js


def test_all_three_domains_are_known_to_the_shell():
    html = _shell()
    m = re.search(r"var DOMAINS = (\[.*?\]);", html, re.S)
    assert m and eval(m.group(1)) == DOMAINS  # noqa: S307 — 自家字面量
    assert "DOMAINS.indexOf(d) >= 0" in SHELL_JS, "壳层没校验兜底目标是不是真域"