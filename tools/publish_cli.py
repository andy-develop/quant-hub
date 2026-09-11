#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HSK 发布 CLI（方案 §6.1，严格按决策 D3）。

D3 状态机（与 tools/publish_hsk.py 的 PublishState 不同 —— 见下方"重要"）：
    RES = vars.HSK_RESOURCE_ID（手工设了优先）→ state/data/hsk-resource.json（CI 自动维护）→ 空
    if RES 为空:  create 一次（hsk-cli host）→ 解析 resource_id+url → 落盘记住
    else:         只 update 这一个（hsk-cli +host --resource-id）
                  成功 -> runlog "ok"
                  失败(403/11301002/超时/任何异常) -> runlog "skipped:<原因>"，exit 0
                  ★ 绝不新建第二个资源、不告警、不开 issue、不影响 Pages

★ 重要（与现有 publish_hsk.py 的口径冲突，需你裁决）：
  tools/publish_hsk.py 的 PublishState.run 实现的是"资源 403 → 自动换新资源并落盘"
  （其落地手册 §7 规则 2）。而方案 D3（§10.1 已拍板）明确要求"失败一律跳过、绝不新建第二个"。
  两者矛盾。本 CLI 按 **D3** 实现（失败即跳过、永不 re-provision）。
  合并页若沿用 publish_hsk.PublishState 会导致"每个故障日换一个 URL"（正是 945q5w→i48ya3→73f9qb 的成因）。
  建议：合并页发布统一走本 CLI；publish_hsk.PublishState 的自动换资源分支按方案 §6.1「要删掉的东西」移除。

Pages 才是主通道（由 build-publish.yml 的 actions/deploy-pages 完成）；HSK 只是 best-effort 镜像，
所以本 CLI **任何情况下都 exit 0**（除非内容文件读取失败），失败只记日志。

用法：
    python -m tools.publish_cli --content dist/index.html --domain merged \
        --resource-file state/data/hsk-resource.json --state state/publish
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.publish_hsk import fingerprint, classify_error  # noqa: E402


# ---------------------------------------------------------------------------
# 纯决策核心（可注入 push/create/verify，离线可测）
# ---------------------------------------------------------------------------
def decide(content: str, *, resource_id: str | None, api_key: str | None,
           last_fingerprint: str | None, push, create, verify=None) -> dict:
    """返回 {action, ...}。action ∈ {skipped, noop, update, create}。严格遵守 D3。"""
    if not api_key:
        return {"action": "skipped", "reason": "no-api-key",
                "note": "HSK_API_KEY 未配置 —— best-effort 镜像跳过，Pages 不受影响"}
    fp = fingerprint(content)
    if last_fingerprint and fp == last_fingerprint:
        return {"action": "noop", "reason": "unchanged", "fingerprint": fp}

    if resource_id:
        # 只 update 这一个；任何失败都跳过，绝不新建（D3）
        try:
            out = push(resource_id, content)
        except Exception as e:  # noqa: BLE001
            return {"action": "skipped", "reason": f"push-error:{classify_error(str(e))}",
                    "detail": str(e)[:200], "fingerprint": fp}
        cls = classify_error(out or "")
        # classify_error 对"无已知错误标记"的文本（含成功输出/URL）返回 'unknown'，
        # 对 claimed/1050 返回 'pending'（正常待确认）。两者都不算失败。
        FAIL_CLASSES = {"disabled_resource", "forbidden", "auth", "rate_limit", "timeout"}
        if cls in FAIL_CLASSES:
            # ★ D3：403/11301002 等一律跳过，不 re-provision
            return {"action": "skipped", "reason": f"push-{cls}", "fingerprint": fp,
                    "note": "D3：失败即跳过，绝不新建第二个资源"}
        if verify is not None:
            try:
                ok = verify(resource_id, fp)
            except Exception as e:  # noqa: BLE001
                return {"action": "skipped", "reason": f"verify-error:{classify_error(str(e))}",
                        "fingerprint": fp}
            if not ok:
                return {"action": "skipped", "reason": "verify-failed", "fingerprint": fp}
        return {"action": "update", "ok": True, "resource_id": resource_id, "fingerprint": fp}

    # 无 resource_id：create 一次并记住
    try:
        out = create(content)
    except Exception as e:  # noqa: BLE001
        return {"action": "skipped", "reason": f"create-error:{classify_error(str(e))}",
                "detail": str(e)[:200]}
    new_id = _parse_field(out, "resource_id") or _parse_field(out, "id")
    url = _parse_field(out, "public_url") or _parse_field(out, "url")
    if not new_id:
        return {"action": "skipped", "reason": "create-no-id", "raw": (out or "")[:200]}
    return {"action": "create", "resource_id": new_id, "url": url, "fingerprint": fp}


def _parse_field(text: str, key: str):
    if not text:
        return None
    # 先试 JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and key in obj:
            return str(obj[key])
    except Exception:  # noqa: BLE001
        pass
    # 再试 key=value / "key":"value"
    import re
    m = re.search(rf'"{key}"\s*:\s*"?([^",\n}}]+)"?', text)
    if m:
        return m.group(1).strip()
    m = re.search(rf'{key}[=:\s]+(\S+)', text)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# 真实 hsk-cli 调用（无 hsk-cli / 无网时自然失败 -> decide 记 skipped）
# ---------------------------------------------------------------------------
def _run_cli(args, timeout=120):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def make_push(content_path_holder):
    def push(resource_id, content):
        path = content_path_holder["path"]
        return _run_cli(["hsk-cli", "+host", path, "--resource-id", str(resource_id)])
    return push


def make_create(content_path_holder):
    def create(content):
        path = content_path_holder["path"]
        return _run_cli(["hsk-cli", "host", path])
    return create


# ---------------------------------------------------------------------------
# 资源 ID 与指纹持久化
# ---------------------------------------------------------------------------
def load_resource_id(resource_file: str, env_override: str | None) -> str | None:
    if env_override:
        return env_override.strip() or None
    if resource_file and os.path.exists(resource_file):
        try:
            with open(resource_file, encoding="utf-8") as f:
                obj = json.load(f)
            rid = obj.get("resource_id") or obj.get("merged") or obj.get("id")
            return str(rid) if rid else None
        except Exception:  # noqa: BLE001
            return None
    return None


def save_resource_id(resource_file: str, resource_id: str, url: str | None) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(resource_file)) or ".", exist_ok=True)
    obj = {"resource_id": str(resource_id), "public_url": url,
           "note": "D3：首次成功创建后记住，之后只 update 这一个，失败绝不新建"}
    tmp = resource_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, resource_file)


def load_last_fingerprint(state_file: str) -> str | None:
    if state_file and os.path.exists(state_file):
        try:
            with open(state_file, encoding="utf-8") as f:
                return json.load(f).get("fingerprint")
        except Exception:  # noqa: BLE001
            return None
    return None


def save_state(state_file: str, result: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(state_file)) or ".", exist_ok=True)
    tmp = state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_file)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HSK best-effort 发布（D3）")
    ap.add_argument("--content", required=True, help="要发布的 HTML 文件")
    ap.add_argument("--domain", default="merged")
    ap.add_argument("--resource-file", default="state/data/hsk-resource.json")
    ap.add_argument("--state", default="state/publish")
    ap.add_argument("--api-key", default=os.environ.get("HSK_API_KEY"))
    ap.add_argument("--resource-id-env", default=os.environ.get("HSK_RESOURCE_ID"))
    args = ap.parse_args(argv)

    if not os.path.exists(args.content):
        print(f"[✗] 内容文件不存在: {args.content}")
        return 2
    content = open(args.content, encoding="utf-8").read()

    state_file = os.path.join(args.state, f"{args.domain}.json")
    rid = load_resource_id(args.resource_file, args.resource_id_env)
    last_fp = load_last_fingerprint(state_file)

    holder = {"path": args.content}
    res = decide(content, resource_id=rid, api_key=args.api_key,
                 last_fingerprint=last_fp, push=make_push(holder), create=make_create(holder))
    res["domain"] = args.domain

    if res["action"] == "create" and res.get("resource_id"):
        save_resource_id(args.resource_file, res["resource_id"], res.get("url"))
        print(f"[✓] HSK 首次创建资源并记住: {res['resource_id']} -> {res.get('url')}")
    elif res["action"] == "update":
        print(f"[✓] HSK update 成功: {res['resource_id']}")
    else:
        print(f"[i] HSK {res['action']}: {res.get('reason')}（best-effort，不影响 Pages）")

    if res.get("fingerprint"):
        save_state(state_file, res)
    # ★ best-effort：永远 exit 0（除非内容文件读不到）
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
