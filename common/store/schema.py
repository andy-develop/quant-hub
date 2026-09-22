"""数据层唯一契约定义处。

冻结规则（同时进 CODEOWNERS，任何变更需本文件 owner review）：

| 变更类型                              | 允许 | 说明 |
|---------------------------------------|------|------|
| 新增列（OPTIONAL）                    | ✅   | 旧分区无该列，reader 返回 NaN，上层不受影响 |
| 新增资产类目录（fund/ bond/）         | ✅   | 走同一套 writer + gates，不碰已有分区 |
| 新增 meta 表（北向资金、ETF 份额）    | ✅   | 同上 |
| 新增分区粒度                          | ✅   | 只要 reader 兼容 |
| 改列名/改类型/改单位/改口径           | ❌   | 需升 CONTRACT_VERSION + 双写过渡期 + 三域全部回归 |
| 改 hfq/raw 的历史值                   | ❌   | 冻结不可变；writer 遇已存在日期直接抛异常 |
| 改 retention 数值                     | ⚠️   | 只允许调大；调小需显式确认（会永久删数据，先 export Release） |

三条硬性陷阱（从注释升级为断言，见 tests/contract/）：

1. **volume 单位 = 股**。baostock 是股，腾讯快照 parts[37] 是**万元**。
   混入同一张表会让所有成交量因子（放量滞涨退出、拥挤度）静默失真。
   换算责任在各 vendor 适配器，本层只认「股」。
2. **代码格式**：baostock `sh.600000` vs 腾讯 `1.600000` vs 短代码 `600000`。
   统一必须收进 reader.py 一处，且顺序不能反：**先 code_map 归一，再叠加 fixup 覆盖**。
   顺序颠倒会让覆盖 isin 全 miss -> 整段重复行（2026-09-08 verify_store 已实测）。
3. **hfq 冻结**：历史值永久冻结。重复回补 = 成倍重复行。
   writer 遇到分区内已存在的 (code, date) -> 抛异常而非覆盖。

全收益指数警告（爆炸半径因合并从一个域变三个域）：

    全收益指数（H20269/H30269/H00300）**只有中证官网有**，绝不能用腾讯的价格指数替代。
    ETF 域 v7.12「地基修正」就是错用价格指数计价漏掉全部分红，导致策略收益
    从 +285.3% 被系统性低估为 +168.2%。
"""

from __future__ import annotations

CONTRACT_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 列定义（冻结项）
# ---------------------------------------------------------------------------
COLUMNS: dict[str, str] = {
    "code": "string[pyarrow]",      # 短代码 600000 / 512890 / H20269
    "date": "date32",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "int64",              # ★单位=股（baostock 是股；腾讯快照 parts[37] 是万元）
    "amount": "float64",            # 元
}

# 可选列：仅部分资产类有。旧分区无该列时 reader 返回 NaN。
OPTIONAL: dict[str, str] = {
    "turnover": "float64",          # 仅 etf / index 有
}

# weekly / monthly 派生分区必带；用于剔除未完成 bar（见 FREQ 与 §is_partial 说明）
PARTIAL_FLAG = "is_partial"

# 主键（用于无重复键断言与 writer 的冻结检查）
PRIMARY_KEY = ("code", "date")

# ---------------------------------------------------------------------------
# 不变量（契约测试逐条断言）
# ---------------------------------------------------------------------------
INVARIANTS: tuple[str, ...] = (
    "close_not_null_positive",      # close 非空且 > 0
    "no_duplicate_keys",            # 无 (code, date) 重复
    "date_in_trade_calendar",       # 日期必须是交易日（需 calendar，见 reader.load_calendar）
    "monotonic_within_code",        # 单 code 内日期单调递增
    "open_high_low_close_consistent",  # low <= min(o,c) <= max(o,c) <= high
)

# ---------------------------------------------------------------------------
# 复权口径
# ---------------------------------------------------------------------------
FQ: dict[str, str] = {
    "hfq": "冻结·只 append 新日期",                       # 信号/回测/因子，历史值永久冻结
    "raw": "冻结·只 append",                              # 涨跌停判定 + volume/amount 真实值
    "qfq": "已废弃（见方案 §0.3：除权处假跳变，动量最高偏差 1.75pp）",
}

# ---------------------------------------------------------------------------
# 频率与派生
# ---------------------------------------------------------------------------
# daily 是唯一权威抓取源；weekly/monthly 由 daily 确定性重采样，物化入库但标记 derived。
#
# 为什么不处理 is_partial 是正确性问题而不是优化项：
#   ETF 域 §31 H-1 整改实测 —— build_signals 若不剔除「未完成 ISO 周」末日行，
#   实盘每日运行末日=今天（未完成周）命中本周 J，回测 ffill 上一周，
#   **97.7% 周中日期信号不同**。
#
# 判定必须用交易日历，禁止 weekday>=4 降级 —— 长假尤其要紧：国庆前最后一根周 bar
# 若被当成完整周，周线 KDJ/RSI 会连续错一整周。
FREQ: dict[str, dict] = {
    "daily": {"derived": False},
    "weekly": {
        "derived": True,
        "rule": "ISO-week|ME|closed-only|calendar-aware",
        # ISO 周（周一起）；按周五对齐会差一根
    },
    "monthly": {
        "derived": True,
        "rule": "calendar-month|ME|closed-only|calendar-aware",
        # 必须用 "ME"：pandas 2.2 弃用 "M"、3.0 移除。
        # 短线域 pandas==3.0.5 与 ETF 域 pandas<2.3 读同一份 weekly 时口径不能分叉。
    },
}

AGGREGATOR_VERSION = "common/aggregate.py@v1"

# ---------------------------------------------------------------------------
# 保留期（★交易日，不是自然日）
# ---------------------------------------------------------------------------
# 为什么用交易日：长假会让"5 自然年"与"1250 交易日"差 5–8 天。
#
# ★ 指数保留期的资产分工（grill-me Q2 显式确认，2026-09-13）：
#   普通指数（中证 H20269/H30269/H00300/000300 等）统一走 asset="etf" = 2430 交易日（10 年）；
#   asset="index" 只装两个基准大盘（BROAD_INDEX_CODES：sh000001/sz399001），全历史、不过期，
#   是短线策略的对比基准，绝不可删。若未来把普通指数并入 asset="index"，expire 的
#   "删整月目录"会连基准全史一起删（零重写原则冲突），必须先给 expire 加 code 级排除。
#
# ★ stock=1250 交易日（5 年）——Q1 用户决策（2026-09-13）：原 730 交易日（3 年）会被日级
#   增量 expire 删掉回测起点 2023-09-01 所需的 hfq 预热窗口（ret120 指标需 2023-01 起），
#   导致 shadow_diff（§7.3）永远无法逐位晋级。回补 2023-01..08 后调大到 5 年：既保住
#   预热窗口（120 日指标 + 3 年回测），又符合 Q5「日级增量顺手删」精神（仍会删更早的旧数据）。
RETENTION: dict[str, int | None] = {
    "stock": 1250,
    "etf": 2430,     # 10 年；体积仅 4MB，"ETF 保留期可以给得很宽松，不用犹豫"
    "index": None,   # ★ 仅基准大盘全历史（Q2 显式确认）；普通指数在 etf=2430
}

# ---------------------------------------------------------------------------
# 资产类
# ---------------------------------------------------------------------------
ASSETS: tuple[str, ...] = ("stock", "etf", "index")

# 域标识（domain= 参数强制，跨域读=口径污染）
DOMAINS: tuple[str, ...] = ("shortterm", "etf", "selected")

# 域私有 meta：禁止跨域读。CI 静态检查 domains/etf/** 不得读 meta/st_history
DOMAIN_PRIVATE_META: dict[str, tuple[str, ...]] = {
    "shortterm": ("st_history",),
    "etf": (),
    "selected": (),
}

# 大盘多周期（方案 §2.7）—— 一等公民，全历史、不做过期
BROAD_INDEX_CODES: tuple[str, ...] = ("sh000001", "sz399001")

# 基准 / 全收益（★全收益只有中证官网有）
INDEX_SPECIAL: dict[str, str] = {
    "H20269": "中证红利全收益（只有 csindex 有，绝不可用价格指数替代）",
    "H30269": "中证红利低波全收益（同上）",
    "H00300": "沪深300全收益（同上）",
    "000300": "沪深300价格指数",
    "sh000300": "沪深300（腾讯源）",
    "sh000852": "中证1000（腾讯源）",
}


# ---------------------------------------------------------------------------
# 事件型数据表（2026-09-22 新增，独立于 K 线资产类）
# ---------------------------------------------------------------------------
# 事件型数据（龙虎榜 / 涨停复盘）主键不是 (code, date)，不能进 market/<asset> 分区：
#   - 龙虎榜一票多因，同 (date, code) 可多次上榜，锚点是东财 TRADE_ID
#   - 涨停复盘是"全市场某日状态"，天然按日聚合
# 统一放 data/events/<table>/（封存 year=/month=/batch= + _incr/YYYYMMDD/），
# 读写走 common/store/events.py，manifest 放 data/manifest/events_<table>.json。
# 列类型与 COLUMNS 同风格（pyarrow 标注）。
EVENT_TABLES: dict[str, dict] = {
    "lhb_detail": {
        "title": "龙虎榜主表（东财 RPT_DAILYBILLBOARD_DETAILSNEW，全历史回补+日增量）",
        "primary_key": ("date", "code", "trade_id"),
        "columns": {
            "date": "date32",
            "code": "string[pyarrow]",          # 短代码 600000
            "name": "string[pyarrow]",
            "market": "string[pyarrow]",        # SH / SZ / BJ
            "trade_id": "int64",                # 东财事件 ID（一票多因的锚点）
            "reason": "string[pyarrow]",        # 上榜原因（EXPLANATION 文本）
            "reason_tag": "string[pyarrow]",    # 上榜说明（EXPLAIN，如"实力游资买入"）
            "change_type": "string[pyarrow]",   # CHANGE_TYPE 内部编码
            "close": "float64",
            "change_rate": "float64",
            "turnover_rate": "float64",
            "free_market_cap": "float64",
            "acc_amount": "float64",            # 当日成交额（元）
            "buy_amount": "float64",            # 龙虎榜买入额（元）
            "sell_amount": "float64",           # 龙虎榜卖出额（元）
            "net_amount": "float64",            # 净买额（元）
        },
    },
    "lhb_seat": {
        "title": "龙虎榜买卖席位明细（东财 RPT_BILLBOARD_DAILYDETAILSBUY/SELL，全历史）",
        "primary_key": ("date", "code", "trade_id", "side", "seat_code"),
        "columns": {
            "date": "date32",
            "code": "string[pyarrow]",
            "trade_id": "int64",
            "side": "string[pyarrow]",          # buy / sell
            "seat_code": "string[pyarrow]",     # OPERATEDEPT_CODE（营业部代码）
            "seat_name": "string[pyarrow]",     # 营业部名称（TOP5 从这里取）
            "buy": "float64",
            "sell": "float64",
            "net": "float64",
            "rank": "int64",                    # 当日该 code+trade_id+side 组内按买卖额降序排名
        },
    },
    "zt_pool": {
        "title": "涨停池快照（东财 push2ex getTopicZTPool，仅近 ~10 交易日）",
        "primary_key": ("date", "code"),
        "columns": {
            "date": "date32",
            "code": "string[pyarrow]",
            "name": "string[pyarrow]",
            "price": "float64",
            "change_rate": "float64",
            "amount": "float64",                # 成交额（元）
            "free_market_cap": "float64",
            "total_market_cap": "float64",
            "turnover_rate": "float64",
            "seal_amount": "float64",           # 封单资金 fund（元）
            "first_seal_time": "int64",         # 首次封板时间 fbt（HHMMSS）
            "last_seal_time": "int64",          # 最后封板时间 lbt（HHMMSS）
            "break_count": "int64",             # 炸板次数 zbc
            "limit_board_count": "int64",       # 连板数 lbc
            "zt_days": "int64",                 # zttj.days（N天M板中的 N）
            "zt_count": "int64",                # zttj.ct（N天M板中的 M）
            "industry": "string[pyarrow]",      # 行业板块 hybk
        },
    },
    "zt_daily": {
        "title": "涨停自算（raw 日K派生，全历史回补+日增量）",
        "primary_key": ("date", "code"),
        "columns": {
            "date": "date32",
            "code": "string[pyarrow]",
            "name": "string[pyarrow]",
            "close": "float64",
            "change_rate": "float64",
            "limit_up": "int64",                # 0/1 是否涨停（按板块阈值）
            "limit_count": "int64",             # 连续涨停数（当日非涨停=0）
            "m3": "int64",                      # 近3交易日涨停次数（含当日）
            "m5": "int64",                      # 近5交易日涨停次数
            "m10": "int64",                     # 近10交易日涨停次数
            "threshold": "float64",             # 当日涨停阈值（%：10/20/30）
        },
    },
    "zt_ladder": {
        "title": "连板梯队/晋级率（自算派生，全历史）",
        "primary_key": ("date", "lbc"),
        "columns": {
            "date": "date32",
            "lbc": "int64",                     # 连板数（1=首板）
            "count": "int64",                   # 当日该连板数家数
            "prev_count": "int64",              # 前一交易日（lbc-1）板家数
            "promote_rate": "float64",          # 晋级率 = 今日 lbc 板家数 / 昨日 (lbc-1) 板家数
        },
    },
}


def columns_for(asset: str) -> tuple[str, ...]:
    """返回该资产类的合法列集合（必选 + 可选，按需裁剪）。"""
    if asset not in ASSETS:
        raise ValueError(f"unknown asset: {asset!r}, expected one of {ASSETS}")
    return tuple(COLUMNS) + tuple(OPTIONAL)


def check_retention(asset: str, trade_days: int | None) -> None:
    """校验某资产类的实际保留交易日数是否符合契约。

    只允许 >= 契约值（调大）。调小需显式确认（会永久删数据）。
    """
    if asset not in RETENTION:
        raise ValueError(f"unknown asset: {asset!r}")
    want = RETENTION[asset]
    if want is None:
        # ★ 基准指数（asset="index"）契约 = 全历史（grill-me Q2 显式确认）。
        #   严禁传非 None：expire 按整月目录删，会把 1990 起的大盘全史删光。
        if trade_days is not None:
            raise AssertionError(
                f"retention violation: asset={asset} 契约=None（全历史冻结，Q2 基准指数显式确认），"
                f"收到 {trade_days}；如需改保留期必须改 schema.RETENTION + 全员 review"
            )
        return
    if trade_days is None:
        raise AssertionError(
            f"retention violation: asset={asset} 契约={want}，收到 None；"
            f"None=不过期 违背「只允许调大」规则"
        )
    if trade_days < want:
        raise AssertionError(
            f"retention violation: asset={asset} has {trade_days} trade days "
            f"< contract {want} (调小会使历史数据永久丢失; 如需调小须先 export Release 归档并显式确认)"
        )
