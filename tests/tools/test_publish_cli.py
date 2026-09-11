#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_cli.decide 的 D3 状态机回归（注入 fake push/create/verify，离线可测）。

锁死方案 §6.1/D3 的关键行为：
  * 无 API key -> skipped（best-effort，不影响 Pages）
  * 指纹未变 -> noop（幂等，不重复推）
  * 有 resource_id + push 抛 403/11301002 -> skipped，★绝不 create 新资源
  * 无 resource_id + create 成功 -> 解析并返回新 id（首次记住）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tools import publish_cli as PC  # noqa: E402


def test_no_api_key_skips():
    r = PC.decide("<html>x</html>", resource_id=None, api_key=None,
                  last_fingerprint=None, push=lambda *a: "", create=lambda *a: "")
    assert r["action"] == "skipped" and r["reason"] == "no-api-key"


def test_unchanged_fingerprint_noop():
    content = "<html>same</html>"
    fp = PC.fingerprint(content)
    r = PC.decide(content, resource_id="123", api_key="k", last_fingerprint=fp,
                  push=lambda *a: "", create=lambda *a: "")
    assert r["action"] == "noop"


def test_update_success():
    r = PC.decide("<html>v1</html>", resource_id="1789030324819741701", api_key="k",
                  last_fingerprint=None, push=lambda rid, c: "ok 200", create=lambda c: "")
    assert r["action"] == "update" and r["ok"] is True


def test_403_never_reprovisions_D3():
    """★ D3 核心：update 撞 403/禁用 -> skipped，绝不调用 create。"""
    created = {"called": False}

    def push(rid, c):
        return "HTTP 403 11301002 resource disabled"

    def create(c):
        created["called"] = True
        return '{"resource_id":"999"}'

    r = PC.decide("<html>v1</html>", resource_id="123", api_key="k",
                  last_fingerprint=None, push=push, create=create)
    assert r["action"] == "skipped"
    assert created["called"] is False, "D3 违反：失败时竟然新建了资源"


def test_push_exception_skips_not_creates():
    created = {"called": False}

    def push(rid, c):
        raise RuntimeError("connection timeout")

    def create(c):
        created["called"] = True
        return "{}"

    r = PC.decide("<html>v1</html>", resource_id="123", api_key="k",
                  last_fingerprint=None, push=push, create=create)
    assert r["action"] == "skipped" and r["reason"].startswith("push-error")
    assert created["called"] is False


def test_first_create_remembers_id():
    r = PC.decide("<html>v1</html>", resource_id=None, api_key="k", last_fingerprint=None,
                  push=lambda *a: "", create=lambda c: '{"resource_id":"1789030324819741701","public_url":"https://73f9qb.gicp.fun"}')
    assert r["action"] == "create"
    assert r["resource_id"] == "1789030324819741701"
    assert "73f9qb" in (r["url"] or "")


def test_verify_failure_skips():
    r = PC.decide("<html>v1</html>", resource_id="123", api_key="k", last_fingerprint=None,
                  push=lambda rid, c: "ok", create=lambda c: "", verify=lambda rid, fp: False)
    assert r["action"] == "skipped" and r["reason"] == "verify-failed"


def test_resource_file_roundtrip(tmp_path):
    rf = str(tmp_path / "hsk-resource.json")
    assert PC.load_resource_id(rf, None) is None
    PC.save_resource_id(rf, "1789030324819741701", "https://x.gicp.fun")
    assert PC.load_resource_id(rf, None) == "1789030324819741701"
    # 环境变量覆盖优先
    assert PC.load_resource_id(rf, "OVERRIDE") == "OVERRIDE"
