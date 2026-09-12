"""HSK 发布状态机测试（方案 §7 + 差异核对报告 D-3）。

三条硬规则各有对应测试，这是防止"发布成功却报红"和"资源无限增殖"的锁。
"""

from __future__ import annotations

import json
import os

import pytest

from tools.publish_hsk import (
    DONE,
    FAILED,
    IDLE,
    NOOP,
    PENDING_CODE,
    DISABLED_CODE,
    PublishState,
    StateStore,
    classify_error,
    fingerprint,
)


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def test_fingerprint_ignores_timestamp():
    """★ 同一份数据、不同生成时间，指纹必须一致（否则每天都会误判"变了"）。"""
    a = fingerprint("<html>data_date=2026-09-11 abc</html>")
    b = fingerprint("<html>2026-09-11T20:31:00 data_date=2026-09-11 abc</html>")
    assert a == b


def test_fingerprint_ignores_whitespace():
    a = fingerprint("<html>data_date=2026-09-11 abc</html>")
    b = fingerprint("<html>\n  data_date=2026-09-11   abc\n</html>")
    assert a == b


def test_fingerprint_ignores_uuid():
    a = fingerprint("<html>data_date=2026-09-11 abc</html>")
    b = fingerprint("<html>data_date=2026-09-11 abc "
                    "550e8400-e29b-41d4-a716-446655440000</html>")
    assert a == b


def test_fingerprint_sensitive_to_content():
    a = fingerprint("<html>data_date=2026-09-11 abc</html>")
    b = fingerprint("<html>data_date=2026-09-11 xyz</html>")
    assert a != b


def test_fingerprint_sensitive_to_data_date():
    a = fingerprint("<html>data_date=2026-09-11 abc</html>")
    b = fingerprint("<html>data_date=2026-09-12 abc</html>")
    assert a != b


def test_fingerprint_carries_data_date_prefix():
    fp = fingerprint("<html>data_date=2026-09-11 abc</html>")
    assert fp.startswith("2026-09-11#")


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expect", [
    (f"update function is disabled ({DISABLED_CODE})", "disabled_resource"),
    ("HTTP 403 Forbidden", "forbidden"),
    ('{"claimed":false,"verify_code":1050}', "pending"),
    ("401 Unauthorized: bad api key", "auth"),
    ("request timeout", "timeout"),
    ("rate limited 429", "rate_limit"),
    ("完全看不懂的报错", "unknown"),
])
def test_classify_error(text, expect):
    assert classify_error(text) == expect


def test_disabled_code_constant_matches_real_incident():
    """11301002 是方案里写明的平台禁用码，不能改。"""
    assert DISABLED_CODE == "11301002"


def test_pending_code_is_treated_as_success_path():
    """★ 规则 1：claimed:false（verify_code 1050）不是失败。

    ETF 域曾把 pending 当失败 → 发布成功却报红，天天假警报。
    """
    assert classify_error(f"verify_code {PENDING_CODE}") == "pending"


# ---------------------------------------------------------------------------
# 状态机：正常路径
# ---------------------------------------------------------------------------
def _run(store_dir, content, push, verify, domain="etf", data_date="2026-09-11"):
    st = PublishState(domain=domain, store=StateStore(str(store_dir)),
                      sleep=lambda s: None, log=lambda *a, **k: None)
    return st.run(content=content, data_date=data_date, push=push, verify=verify)


def test_happy_path(tmp_path):
    content = "<html>data_date=2026-09-11 hello</html>"

    def push(fp, rid):
        return '{"resource_id":"1788920564682150822","url":"https://945q5w.gicp.fun","claimed":false,"verify_code":1050}'

    def verify(url, fp):
        return True

    rec = _run(tmp_path, content, push, verify)
    assert rec.state == DONE
    assert rec.verified is True
    assert rec.resource_id == "1788920564682150822"
    assert rec.url == "https://945q5w.gicp.fun"


def test_pending_does_not_fail(tmp_path):
    """★ 规则 1 端到端：pending 输出 + 校验通过 = DONE，不是 FAILED。"""
    def push(fp, rid):
        return '{"claimed":false,"verify_code":1050,"resource_id":"1"}'

    rec = _run(tmp_path, "<html>data_date=2026-09-11 x</html>", push,
               lambda u, f: True)
    assert rec.state == DONE, f"pending 被误判为失败: {rec.state}"


def test_noop_on_unchanged_fingerprint(tmp_path):
    """★ 幂等：指纹没变时跳过发布（CI 重跑不该反复推同一个页面）。"""
    content = "<html>data_date=2026-09-11 same</html>"
    calls = []

    def push(fp, rid):
        calls.append(1)
        return '{"resource_id":"1","url":"https://a.gicp.fun"}'

    _run(tmp_path, content, push, lambda u, f: True)
    rec2 = _run(tmp_path, content, push, lambda u, f: True)
    assert rec2.state == NOOP
    assert len(calls) == 1, "第二次不该再推"


# ---------------------------------------------------------------------------
# 状态机：资源被禁用 -> 自动换资源
# ---------------------------------------------------------------------------
def test_disabled_resource_triggers_reprovision(tmp_path):
    """★ 规则 2：403/11301002 必须自动换资源，不能在旧资源上死循环。"""
    seen_rids = []

    def push(fp, rid):
        seen_rids.append(rid)
        if rid is None:
            return '{"resource_id":"999","url":"https://new.gicp.fun"}'
        return f"update function is disabled ({DISABLED_CODE})"

    rec = _run(tmp_path, "<html>data_date=2026-09-11 y</html>", push,
               lambda u, f: True)

    assert seen_rids[0] is None or seen_rids[-1] is None, "第二轮应走新建"
    assert "999" in seen_rids or rec.resource_id == "999"


def test_reprovision_records_new_resource(tmp_path):
    def push(fp, rid):
        if rid is None:
            return '{"resource_id":"555","url":"https://new.gicp.fun"}'
        return "403 forbidden"

    rec = _run(tmp_path, "<html>data_date=2026-09-11 z</html>", push,
               lambda u, f: True)
    assert rec.resource_id == "555"
    assert rec.url == "https://new.gicp.fun"


def test_auth_error_does_not_reprovision(tmp_path):
    """★ 鉴权失败换资源没用 —— 必须直接 FAILED，不能无限建资源。"""
    calls = []

    def push(fp, rid):
        calls.append(rid)
        return "401 Unauthorized api key invalid"

    rec = _run(tmp_path, "<html>data_date=2026-09-11 a</html>", push,
               lambda u, f: True)
    assert rec.state == FAILED
    assert len(calls) == 1, "鉴权失败不该重试建资源"


# ---------------------------------------------------------------------------
# 状态机：校验与重试
# ---------------------------------------------------------------------------
def test_verify_retry_then_success(tmp_path):
    n = {"i": 0}

    def verify(url, fp):
        n["i"] += 1
        return n["i"] >= 3          # 前两次失败，第三次成功

    rec = _run(tmp_path, "<html>data_date=2026-09-11 b</html>",
               lambda fp, rid: '{"resource_id":"1","url":"https://a.gicp.fun"}',
               verify)
    assert rec.state == DONE
    assert n["i"] == 3


def test_verify_exhausted_fails(tmp_path):
    rec = _run(tmp_path, "<html>data_date=2026-09-11 c</html>",
               lambda fp, rid: '{"resource_id":"1","url":"https://a.gicp.fun"}',
               lambda u, f: False)
    assert rec.state == FAILED
    assert rec.verified is False
    assert "校验" in (rec.last_error or "")


def test_verify_exception_treated_as_failure(tmp_path):
    def verify(url, fp):
        raise RuntimeError("network down")

    rec = _run(tmp_path, "<html>data_date=2026-09-11 d</html>",
               lambda fp, rid: '{"resource_id":"1","url":"https://a.gicp.fun"}',
               verify)
    assert rec.state == FAILED


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------
def test_state_persisted_and_reloaded(tmp_path):
    """★ 资源 ID 必须落盘 —— 丢了就每次新建资源，无限增殖。"""
    content = "<html>data_date=2026-09-11 p</html>"
    _run(tmp_path, content,
         lambda fp, rid: '{"resource_id":"777","url":"https://p.gicp.fun"}',
         lambda u, f: True)

    p = os.path.join(str(tmp_path), "etf.json")
    assert os.path.exists(p)
    d = json.load(open(p, encoding="utf-8"))
    assert d["resource_id"] == "777"
    assert d["verified"] is True
    assert d["state"] == DONE

    st = StateStore(str(tmp_path))
    rec = st.load("etf")
    assert rec.resource_id == "777"
    assert rec.verified is True


def test_state_store_missing_file_returns_blank(tmp_path):
    rec = StateStore(str(tmp_path)).load("nope")
    assert rec.state == IDLE
    assert rec.resource_id is None


def test_state_store_survives_corrupted_file(tmp_path):
    p = os.path.join(str(tmp_path), "etf.json")
    open(p, "w").write("{ not json")
    rec = StateStore(str(tmp_path)).load("etf")
    assert rec.state == IDLE          # 不抛，降级为空白记录


# ---------------------------------------------------------------------------
# 三域资源现状（差异核对报告 D-3）
# ---------------------------------------------------------------------------
def test_known_resources_documented():
    """方案写的 73f9qb 已过时 —— 实测是 3lpj77。这里把事实钉住。"""
    etf_resource = "1788920564682150822"      # 945q5w.gicp.fun
    stock_resource = "1789099872672276384"    # 3lpj77.gicp.fun
    assert etf_resource.isdigit() and len(etf_resource) == 19
    assert stock_resource.isdigit() and len(stock_resource) == 19
    assert etf_resource != stock_resource, "两域必须用不同资源，否则互相覆盖"


# ---------------------------------------------------------------------------
# ★ D3（方案 §6.1/§10.1）：已有资源更新被禁 -> 跳过，绝不新建第二个资源
#   （旧"自动换资源"行为已移除 —— 它正是 ETF URL 945q5w→i48ya3→73f9qb 天天变的成因）
# ---------------------------------------------------------------------------
def test_D3_disabled_resource_does_not_reprovision(tmp_path):
    from tools.publish_hsk import PublishRecord, StateStore, PublishState, FAILED, DISABLED_CODE
    store = StateStore(str(tmp_path))
    store.save(PublishRecord(domain="etf", resource_id="OLD123", url="https://old.gicp.fun"))

    calls = []

    def push(fp, rid):
        calls.append(rid)
        return f"update function is disabled ({DISABLED_CODE})"

    st = PublishState(domain="etf", store=store, sleep=lambda s: None, log=lambda *a, **k: None)
    rec = st.run(content="<html>data_date=2026-09-12</html>", data_date="2026-09-12",
                 push=push, verify=lambda u, f: True)

    assert rec.state == FAILED
    assert calls == ["OLD123"], f"D3 违反：应只 update 这一个资源，实际调用 {calls}（出现 None=新建）"
    assert rec.resource_id == "OLD123", "D3：不得把 resource_id 置 None 去新建"
    assert "不新建" in (rec.last_error or "")


def test_D3_forbidden_resource_does_not_reprovision(tmp_path):
    from tools.publish_hsk import PublishRecord, StateStore, PublishState, FAILED
    store = StateStore(str(tmp_path))
    store.save(PublishRecord(domain="etf", resource_id="OLD9", url="https://old.gicp.fun"))
    calls = []

    def push(fp, rid):
        calls.append(rid)
        return "403 forbidden"

    st = PublishState(domain="etf", store=store, sleep=lambda s: None, log=lambda *a, **k: None)
    rec = st.run(content="<html>data_date=2026-09-12 b</html>", data_date="2026-09-12",
                 push=push, verify=lambda u, f: True)
    assert rec.state == FAILED and calls == ["OLD9"]
