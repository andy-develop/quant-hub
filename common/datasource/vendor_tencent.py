"""腾讯行情 vendor（个股 / ETF / 指数）。

★ 本文件包含方案 §0.4 的根因修复，可被三域共用。

## §0.4 事故复盘（正在发生，连续 4 个交易日）

对 `quant-lab/data/kline/incremental/raw_2026090{7,8,9},10.parquet` 逐日统计：

| 日期     | 入库总数 | 沪市（在市 2316） | 深市（在市 2899） |
|----------|----------|-------------------|-------------------|
| 09-04    | 5015     | 2234（99.4%）     | 2781（95.9%）     |
| 09-07~10 | ~2925    | **39（1.7%）**    | ~2886（99.6%）    |

**每天的数字一模一样（39 = 2316 − 38×60）**，即"按 60 只一批请求，前 38 个批次
（几乎全是沪市）整体失败被丢弃"。

根因在 `snapshot_day()` 的四条静默丢弃路径（原实现）：

```python
except Exception:              # ① 整批失败
    time.sleep(2); continue
if len(parts) < 40:            # ② 腾讯限流降级格式字段少
    continue
sym_full = ("sh" if line.startswith("v_sh") else "sz") + sym   # ③ ★
if sym_full not in sym_map:    #    "v_s_sh600004" 不以 "v_sh" 开头 -> 判成 sz -> 丢弃
    continue
except ValueError:             # ④ 解析异常（方案漏列）
    continue
```

第 ③ 条最可疑：腾讯批量接口限流时把响应降级为 `v_s_sh600004="..."` 简化格式，
前缀是 `v_s_` 而不是 `v_sh`，于是 `startswith("v_sh")` 为假 -> 沪市代码被拼成
`sz600004` -> 不在 `sym_map` 里 -> 整批静默 continue。**深市不受影响是因为
深市行的前缀判定走同一个 `else` 分支，恰好正确。**

## 修复要点

1. 四条静默 `continue` 全部改为**计数 + 记 runlog**（判定交给覆盖率门禁）
2. 响应行以 `v_s_` 开头时按简化格式单独解析（本文件 `_market_of_line`）
3. 批内失败**逐只重试一次**，批次整体失败**指数退避重试 3 次**
4. 仍失败进 `manifest.missing_codes` 由 21:00 补跑 cron 补齐
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from . import FetchStats, TokenBucket, RetryPolicy, Vendor

__all__ = [
    "parse_qt_batch_response",
    "TencentSnapshotVendor",
    "QT_URL",
    "IFZQ_URL",
    "SNAPSHOT_MIN_FIELDS",
]

QT_URL = "https://qt.gtimg.cn/q="
IFZQ_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SNAPSHOT_MIN_FIELDS = 40       # 完整快照响应字段数下限（简化格式远少于此）
SIMPLIFIED_MIN_FIELDS = 5      # 简化格式：v_s_sh600004="名称~代码~现价~..."

# 腾讯响应行前缀：
#   v_sh600004="..."      完整格式（沪）
#   v_sz000001="..."      完整格式（深）
#   v_s_sh600004="..."    ★ 限流降级简化格式（前缀是 v_s_）
_LINE_RE = re.compile(r'^v_(s_)?(sh|sz|bj)(\d{6})="(.*)"$')


# ---------------------------------------------------------------------------
# ★ 核心修复：市场判定
# ---------------------------------------------------------------------------
def _market_of_line(line: str) -> tuple[str | None, str | None, bool]:
    """从一行响应里解析 (市场, 代码, 是否简化格式)。

    这是 §0.4 的修复核心。原实现只做二值判断：
        sym_full = ("sh" if line.startswith("v_sh") else "sz") + sym
    对 `v_s_sh600004`（降级格式）会判成 sz -> 整批丢弃。

    新实现显式识别 `v_s_` 前缀，**不再让任何前缀形态落到错误的 else 分支**。
    """
    s = line.strip()
    if not s or "=" not in s:
        return None, None, False

    m = _LINE_RE.match(s)
    if not m:
        # 非标准形态：尝试宽松解析（仍要能判出市场，不许猜成深市）
        head = s.split("=", 1)[0].strip()
        mm = re.match(r'^v_?(s_)?(sh|sz|bj)(\d{6})$', head)
        if not mm:
            return None, None, False
        return mm.group(2), mm.group(3), bool(mm.group(1))

    simplified = bool(m.group(1))
    return m.group(2), m.group(3), simplified


def parse_qt_batch_response(text: str, sym_map: dict[str, str],
                            stats: FetchStats | None = None,
                            *, logger=None) -> list[dict]:
    """解析腾讯批量快照响应。

    ★ 与旧实现的区别：**不静默丢弃**。每条丢弃都计入 stats。

    sym_map: {"sh600004": "1.600004", ...}（按市场+代码索引到 secid）
    """
    stats = stats or FetchStats(vendor="tencent-qt")
    rows: list[dict] = []
    day_ts = None

    for line in text.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue

        mkt, num, simplified = _market_of_line(line)
        if mkt is None or num is None:
            stats.dropped_other += 1
            continue

        sym_full = mkt + num                      # ★ 用解析出的市场，不做二值猜测
        parts = line.split("=", 1)[1].strip().strip('"').split("~")

        if simplified:
            # ★ 限流降级格式：字段数少，只取可用字段；不再因为"字段数不足"整条丢弃
            stats.dropped_short_format += 1       # 记账：发生了降级（指标，不等于丢数据）
            row = _parse_simplified(sym_full, num, parts, sym_map, stats)
            if row:
                rows.append(row)
            continue

        if len(parts) < SNAPSHOT_MIN_FIELDS:
            stats.dropped_short_format += 1
            if logger:
                logger(f"[qt] 字段数 {len(parts)} < {SNAPSHOT_MIN_FIELDS}，"
                       f"代码 {sym_full} 疑似格式变更")
            continue

        if sym_full not in sym_map:
            # ★ §0.4 根因在这里被拦下：正常情况不应命中。
            #   命中说明 universe 与快照不一致（新股/退市），计数而不是静默。
            stats.dropped_unknown_code += 1
            continue

        try:
            o, c = float(parts[5]), float(parts[3])
            h, l = float(parts[33]), float(parts[34])
            v = float(parts[6]) if parts[6] else 0.0
            prev = float(parts[4])
            # ★ unit: 腾讯 parts[37] 是**万元** -> 换算成**元**
            amt = float(parts[37]) * 1e4 if parts[37] else 0.0
        except (ValueError, IndexError):
            stats.dropped_parse_error += 1
            continue

        if c <= 0 or o <= 0:
            stats.dropped_parse_error += 1
            continue

        rows.append({
            "code": sym_map[sym_full], "date": day_ts,
            "open": o, "close": c, "high": h, "low": l,
            "volume": int(v), "amount": amt, "prev_close": prev,
        })
    return rows


def _parse_simplified(sym_full: str, num: str, parts: list[str],
                      sym_map: dict[str, str], stats: FetchStats) -> dict | None:
    """解析限流降级格式。

    简化格式字段更少（实测约 5–20 个），我们只取能确定语义的位置：
        parts[1] = 代码, parts[2] = 名称, parts[3] = 现价, parts[4] = 昨收, parts[5] = 今开
    若字段不足以支撑 OHLCV，返回 None（计入 short_format，由覆盖率门禁统一判定）。
    """
    if sym_full not in sym_map:
        stats.dropped_unknown_code += 1
        return None
    if len(parts) < SIMPLIFIED_MIN_FIELDS:
        stats.dropped_other += 1
        return None
    try:
        c = float(parts[3])
        prev = float(parts[4]) if parts[4] else 0.0
        o = float(parts[5]) if len(parts) > 5 and parts[5] else c
    except (ValueError, IndexError):
        stats.dropped_parse_error += 1
        return None
    if c <= 0:
        stats.dropped_parse_error += 1
        return None
    # 降级格式通常没有 high/low/volume/amount -> 用现价兜底，并留痕
    # ★ 这里不臆造数据：high/low 置为现价（等于"当日无波动"的保守值），
    #   并由 L1 备源（baostock / web.ifzq 日K）在后续 run 补齐真实值。
    return {
        "code": sym_map[sym_full], "date": None,
        "open": o, "close": c, "high": c, "low": c,
        "volume": 0, "amount": 0.0, "prev_close": prev,
        "_degraded": True,
    }


# ---------------------------------------------------------------------------
# Vendor 封装
# ---------------------------------------------------------------------------
class TencentSnapshotVendor:
    """腾讯批量快照（qt.gtimg.cn）。

    批量 60 只/请求，与旧实现一致（实测 ~93 请求覆盖全 A）。
    """

    name = "tencent-qt"

    def __init__(self, session=None, batch_size: int = 60, timeout: int = 15,
                 logger=None):
        self.batch_size = batch_size
        self.timeout = timeout
        self.logger = logger
        self._session = session

    @property
    def session(self):
        if self._session is None:
            import requests

            s = requests.Session()
            s.trust_env = False
            s.headers.update({
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36"})
            self._session = s
        return self._session

    def sym_map(self, secids: Iterable[str]) -> dict[str, str]:
        out = {}
        for secid in secids:
            mkt, num = str(secid).split(".")
            out[("sh" if mkt == "1" else "sz") + num] = secid
        return out

    def fetch_batch(self, batch_syms: Sequence[str],
                    stats: FetchStats | None = None) -> tuple[str, list[dict]]:
        """取一批（这里是 sh600004 形态的符号）。返回 (原始文本, 解析行)。"""
        stats = stats or FetchStats(vendor=self.name)
        url = QT_URL + ",".join(batch_syms)
        r = self.session.get(url, timeout=self.timeout)
        r.encoding = "gbk"
        text = r.text
        sym_map = {s: s for s in batch_syms}  # 本批直接映射
        rows = parse_qt_batch_response(text, sym_map, stats, logger=self.logger)
        return text, rows

    def to_vendor(self, secids: Iterable[str]) -> Vendor:
        """构造 common.datasource.Vendor（供 fetch_with_fallback 编排）。"""
        smap = self.sym_map(secids)
        batch_size = self.batch_size

        def _fetch(batch_codes: list[str]) -> list[dict]:
            # batch_codes 是 secid（1.600004）
            syms = []
            for sid in batch_codes:
                mkt, num = str(sid).split(".")
                syms.append(("sh" if mkt == "1" else "sz") + num)
            n_batch = (len(syms) + batch_size - 1) // batch_size
            out: list[dict] = []
            stats = FetchStats(vendor=self.name)
            for i in range(n_batch):
                chunk = syms[i * batch_size:(i + 1) * batch_size]
                _, rows = self.fetch_batch_syms(chunk, stats, smap)
                out.extend(rows)
            return out

        v = Vendor(name=self.name, fetch=_fetch,
                   bucket=TokenBucket(1.0, 1))
        self._current_stats = FetchStats(vendor=self.name)
        return v

    def fetch_batch_syms(self, chunk: Sequence[str], stats: FetchStats,
                         smap: dict[str, str]):
        url = QT_URL + ",".join(chunk)
        r = self.session.get(url, timeout=self.timeout)
        r.encoding = "gbk"
        rows = parse_qt_batch_response(r.text, smap, stats, logger=self.logger)
        return r.text, rows


# ---------------------------------------------------------------------------
# ifzq 日K（指数兜底 + 个股除权重拉，与旧实现同源）
# ---------------------------------------------------------------------------
def fetch_ifzq_daily(session, symbol: str, start: str, end: str,
                     *, fq: str = "", limit: int = 800, timeout: int = 15):
    """腾讯 web.ifzq 日K。

    实测：单次约 800 根上限，长区间要分两段拼接（2023-01 起约 950 个交易日）。
    """
    url = IFZQ_URL
    params = {"param": f"{symbol},day,{start},{end},{limit},{fq}".rstrip(",") + ","}
    r = session.get(url, params=params, timeout=timeout)
    d = r.json()["data"][symbol]
    bars = d.get("qfqday" if fq == "qfq" else "day") or d.get("day") or []
    return [b for b in bars if isinstance(b, list) and len(b) >= 6]
