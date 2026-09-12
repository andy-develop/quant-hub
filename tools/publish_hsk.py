"""HSK 发布状态机（方案 §7）。

## 背景（实测三仓的发布现状）

三域各写各的发布逻辑，且**踩过同一类坑**：

| 域 | 资源 | 现状 |
|----|------|------|
| ETF | `1788920564682150822` -> `945q5w.gicp.fun` | ✅ 有重试+校验+资源持久化 |
| 个股 | `1789099872672276384` -> `3lpj77.gicp.fun` | ⚠️ 直接 `hsk-cli +host`，无校验 |
| 短线 | ~~`kpqv8z.gicp.fun`~~ | ❌ 平台禁用更新（11301002），报告已改本地发布 |

方案原文写的 `73f9qb` 是**过时信息** —— 实测资源已换成 `3lpj77`。
详见 `差异核对报告.md` D-3。

## 状态机

发布不是"调一次 CLI"，而是一条**可重入、可回滚、有终态**的流水线：

```
  IDLE
    │  build 产出 index.html + 期望指纹
    ▼
  READY ──(无变更)──► NOOP ──► DONE
    │  push
    ▼
  PUSHING ──(403 / 11301002 资源被禁用)──► REPROVISION ──► PUSHING(新资源)
    │                     │
    │                     └─(创建也失败)──► FAILED
    │  claimed:false / pending (正常, 内容已生效)
    ▼
  VERIFYING ──(内容指纹不符)──► RETRY (最多 N 次, 指数退避)
    │                              │
    │                              └─(重试耗尽)──► FAILED
    ▼
  DONE   (记录 resource_id / url / 指纹 到 state)
```

## 三条硬规则

1. **`claimed:false` / `pending` 不是失败** —— verify_code 1050 属正常待确认，
   内容即时生效。把它当失败会导致"发布成功却报红"的假警报（ETF 域踩过）。
2. **资源 403 `11301002` 必须自动换资源**，且新资源 ID 要**持久化并提交**，
   否则下次 run 又打旧资源、又 403、又建新资源 —— 资源无限增殖。
3. **发布后必须回读校验内容指纹**（不是只看 HTTP 200）。
   平台可能返回缓存/旧版本，HTTP 200 什么都证明不了。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field

__all__ = [
    "PublishState",
    "PublishRecord",
    "StateStore",
    "fingerprint",
    "classify_error",
    "DISABLED_CODE",
    "build_once",
]

TZ_CST = _dt.timezone(_dt.timedelta(hours=8))
DISABLED_CODE = "11301002"          # 平台禁用已建资源的内容更新
PENDING_CODE = "1050"               # verify_code 1050 = 待确认（正常）

# 状态
IDLE, READY, NOOP, PUSHING, REPROVISION, VERIFYING, RETRY, DONE, FAILED = (
    "idle", "ready", "noop", "pushing", "reprovision", "verifying", "retry",
    "done", "failed")

TERMINAL = {DONE, FAILED, NOOP}
MAX_VERIFY_RETRY = 3


# ---------------------------------------------------------------------------
# 内容指纹
# ---------------------------------------------------------------------------
def fingerprint(html_or_bytes) -> str:
    """内容指纹：发布校验的唯一凭据。

    ★ 不能只看"页面能打开" —— 平台返回旧版本 / CDN 缓存都会 200。
      指纹取 `data_date + 结构摘要`，比全文件哈希更稳（时间戳/随机 id 不影响）。
    """
    if isinstance(html_or_bytes, bytes):
        data = html_or_bytes
    else:
        data = str(html_or_bytes).encode("utf-8")
    txt = data.decode("utf-8", "ignore")

    # 抽语义摘要：数据日（最稳的"这份数据是哪天的"凭据）
    m = re.search(r'data[_-]?date["\']?\s*[:=]\s*["\']?(20\d{2}-\d{2}-\d{2})', txt)
    if not m:
        m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", txt)
    day = m.group(1) if m else ""

    # 剔除易变片段后再哈希
    #   1) 删掉时间戳/UUID
    #   2) 白空格收敛：先把所有空白压成单空格，再删掉标签相邻的空白
    #      （只压不删会留 `<html> ` vs `<html>` 的差异，指纹就不稳了）
    t = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?([+-]\d{2}:?\d{2}|Z)?', "", txt)
    t = re.sub(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', "", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s*([<>{};,])\s*", r"\1", t)
    t = t.strip()
    h = hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]
    return f"{day}#{h}" if day else h


def classify_error(text: str) -> str:
    """把 hsk-cli / HTTP 的输出分类成状态机可处理的错误类型。"""
    t = str(text or "")
    if DISABLED_CODE in t or "disabled" in t.lower():
        return "disabled_resource"      # -> REPROVISION
    if PENDING_CODE in t or "claimed" in t.lower() and "false" in t.lower():
        return "pending"                # -> 正常，继续 VERIFYING
    if "403" in t or "forbidden" in t.lower():
        return "forbidden"              # -> REPROVISION
    if "401" in t or "unauthorized" in t.lower() or "api key" in t.lower():
        return "auth"                   # -> FAILED（密钥问题，换资源没用）
    if "timeout" in t.lower() or "timed out" in t.lower():
        return "timeout"                # -> RETRY
    if "429" in t or "rate" in t.lower():
        return "rate_limit"             # -> RETRY
    return "unknown"                    # -> RETRY（保守：可重试）


# ---------------------------------------------------------------------------
# 资源记录
# ---------------------------------------------------------------------------
@dataclass
class PublishRecord:
    """一次发布的结果记录（落 `state/publish/{domain}.json`）。"""

    domain: str
    state: str = IDLE
    resource_id: str | None = None
    url: str | None = None
    fingerprint: str | None = None
    data_date: str | None = None
    attempts: int = 0
    created_resources: list[str] = field(default_factory=list)
    last_error: str | None = None
    error_kind: str | None = None
    verified: bool = False
    updated_at: str = ""

    def touch(self) -> None:
        self.updated_at = _dt.datetime.now(TZ_CST).isoformat(timespec="seconds")

    def to_dict(self) -> dict:
        return asdict(self)


class StateStore:
    """`state/publish/{domain}.json` 的读写。

    ★ 必须持久化并提交进仓库：资源 ID 丢了就会每次 run 新建资源，
      资源无限增殖，且用户手里的旧链接永久失效。
    """

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def path(self, domain: str) -> str:
        return os.path.join(self.root, f"{domain}.json")

    def load(self, domain: str) -> PublishRecord:
        p = self.path(domain)
        if not os.path.exists(p):
            return PublishRecord(domain=domain)
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return PublishRecord(**{k: v for k, v in d.items()
                                    if k in PublishRecord.__dataclass_fields__})
        except Exception:
            return PublishRecord(domain=domain)

    def save(self, rec: PublishRecord) -> str:
        rec.touch()
        p = self.path(rec.domain)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rec.to_dict(), f, ensure_ascii=False, indent=2)
        return p


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
@dataclass
class PublishState:
    """发布状态机。

    把"调 CLI"这件事拆成可断言、可测试的步骤；
    `push` / `verify` 由调用方注入（便于在 CI 里换成真实 hsk-cli，
    在测试里换成假实现）。
    """

    domain: str
    store: StateStore
    max_verify_retry: int = MAX_VERIFY_RETRY
    sleep: callable = time.sleep
    log: callable = print

    def run(self, *, content: str, data_date: str | None = None,
            push: callable, verify: callable,
            expected_fingerprint: str | None = None) -> PublishRecord:
        """跑完整条流水线。

        push(fingerprint, resource_id) -> str
            返回 hsk-cli 的输出文本。resource_id 为 None 表示需要新建。
        verify(url, fingerprint) -> bool
            回读线上内容并比对指纹。
        """
        rec = self.store.load(self.domain)
        rec.state = READY
        rec.data_date = data_date
        fp = expected_fingerprint or fingerprint(content)
        rec.fingerprint = fp
        self.store.save(rec)

        # 幂等：指纹没变且上次已 DONE -> NOOP
        if rec.verified and rec.fingerprint == fp and rec.url:
            rec.state = NOOP
            self.log(f"[{self.domain}] 指纹未变 ({fp})，跳过发布")
            self.store.save(rec)
            return rec

        # ---- PUSHING / REPROVISION ----
        pushed = False
        for round_no in range(2):          # 最多两轮：原资源 -> 新资源
            rec.state = PUSHING if round_no == 0 else REPROVISION
            rec.attempts += 1
            try:
                out = push(fp, rec.resource_id)
            except Exception as e:          # noqa: BLE001
                out = str(e)
            kind = classify_error(out)
            rec.error_kind = kind
            rec.last_error = out[:500]

            if kind in ("disabled_resource", "forbidden"):
                # ★ D3（方案 §6.1「要删掉的东西」/ §10.1 已拍板）：资源更新被平台禁用 ->
                #   best-effort 跳过，**绝不新建第二个资源**。旧实现在这里 rec.resource_id=None; continue
                #   去新建，正是 ETF 域 URL 每天变（945q5w→i48ya3→73f9qb）的成因，按 D3 移除。
                rid = _parse_resource_id(out)
                if rid and rid not in rec.created_resources:
                    rec.created_resources.append(rid)        # 仅留档审计，不据此新建
                rec.state = FAILED
                rec.last_error = (f"D3: 资源 {rec.resource_id} 更新被拒（{kind}）—— "
                                  f"best-effort 跳过，不新建第二个资源（Pages 才是主通道）")
                self.store.save(rec)
                self.log(f"[{self.domain}] {rec.last_error}")
                return rec

            if kind == "auth":
                rec.state = FAILED
                self.log(f"[{self.domain}] 鉴权失败，换资源无用：{out[:200]}")
                self.store.save(rec)
                return rec

            if kind in ("pending", "unknown", "timeout", "rate_limit", ""):
                # ★ 规则 1：pending/claimed:false 是正常态，内容已生效
                rid = _parse_resource_id(out)
                url = _parse_url(out)
                if rid:
                    rec.resource_id = rid
                if url:
                    rec.url = url
                pushed = True
                break

            # 其他未知错误：也当作可能成功，交给 verify 判定
            pushed = True
            break

        if not pushed and rec.resource_id is None:
            # 两轮都没拿到资源
            rec.state = FAILED
            rec.last_error = (rec.last_error or "") + " | 新建资源后仍未取得 resource_id"
            self.store.save(rec)
            return rec

        self.store.save(rec)

        # ---- VERIFYING / RETRY ----
        for i in range(self.max_verify_retry):
            rec.state = VERIFYING if i == 0 else RETRY
            try:
                ok = verify(rec.url, fp)
            except Exception as e:          # noqa: BLE001
                ok = False
                rec.last_error = str(e)[:300]
            if ok:
                rec.verified = True
                rec.state = DONE
                self.store.save(rec)
                self.log(f"[{self.domain}] 发布完成 {rec.url} 指纹 {fp}")
                return rec
            rec.log_retry = i             # type: ignore[attr-defined]
            self.log(f"[{self.domain}] 校验未通过（第 {i+1}/{self.max_verify_retry} 次），退避重试")
            self.sleep(2 ** i)

        rec.state = FAILED
        rec.verified = False
        rec.last_error = f"发布后校验 {self.max_verify_retry} 次均未通过（指纹 {fp}）"
        self.store.save(rec)
        self.log(f"[{self.domain}] 发布失败：{rec.last_error}")
        return rec


_RID_RE = re.compile(r'resource[_-]?id["\']?\s*[:=]\s*["\']?(\d+)', re.I)
_URL_RE = re.compile(r'https?://[a-z0-9.-]+\.gicp\.fun[^\s"\']*', re.I)
_URL_RE2 = re.compile(r'"?url"?\s*[:=]\s*"?(https?://[^\s"\',}]+)', re.I)


def _parse_resource_id(text: str) -> str | None:
    m = _RID_RE.search(text or "")
    return m.group(1) if m else None


def _parse_url(text: str) -> str | None:
    m = _URL_RE.search(text or "") or _URL_RE2.search(text or "")
    return m.group(0) if m else None


def build_once(*, domain: str, state_dir: str, content: str,
               push: callable, verify: callable,
               data_date: str | None = None) -> PublishRecord:
    """便捷入口：构建一个状态机并跑一次。"""
    st = PublishState(domain=domain, store=StateStore(state_dir))
    return st.run(content=content, data_date=data_date, push=push, verify=verify)
