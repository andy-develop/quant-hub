#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7.7 统一回测引擎（可复现 · 产品与回测共用单一实现）

口径修正（相对 v7.6）：
  1. 信号全部用【价格指数 H30269】计算（布林/RSI/KDJ/63日动量），收益用【全收益指数 H20269】——
     消除 TR 指数分红再投向上漂移对择时信号的污染（原 px 抓了不用）。
  2. 执行模型改为 T+1：T 日收盘确认信号，T+1 日收盘价成交。
     披露：数据源无开盘价，以 T+1 收盘近似 T+1 开盘；另加单边滑点 5bp 保守化。
  3. 融资成本：仓位 >100% 的杠杆部分（25%/50%）按年化 7% 按交易日计息（0.07/252/日）。
  4. 费用：单边 max(成交额×万1, 5 元)。
  5. 输出日频全量净值/回撤（2427 个交易日不抽稀），指标在引擎内计算，杜绝静态产物误导。

用法:
  python3 engine.py [--out-dir .] [--start 2016-09-08]   # 独立回测（读 ../backtest 本地 CSV）
  或作为模块被 update.py import（get_prices -> build_signals -> replay -> metrics）
"""
import json, os, sys, math, datetime
import pandas as pd
import numpy as np

# ============ 参数（v7.7 与 v7.6 一致的信号参数，执行口径修正） ============
HOLD_DAYS = 60            # 临时仓到期卖出：60 个自然日
REBUY_DAYS = 90           # A态清仓后强制回补上限：90 个自然日（v7.9: 60→90，减少L型磨底接飞刀）
J_LOW, J_HIGH = 1.0, 95.0
J_CROSS_FROM, J_CROSS_TO = 90.0, 80.0
RSI_OS = 35.0
RSI_CROSS_FROM, RSI_CROSS_TO = 70.0, 65.0
X_UP, Y_DOWN = 20.0, 14.0   # 超买63日涨幅阈值20% / 超卖63日跌幅阈值14%（v7.10: 15→14，walk-forward三段一致增强，夏普0.64/回撤-30.4%）
Y_ACC = False               # 实验否决：超卖跌幅加"跌幅加速"确认（近10日跌幅 ≥ 近63日跌幅×0.6，全期/WF均变差，保留代码可复现）
DIV_2OF3 = False            # 实验否决：顶背离扩展 RSI+MACD+量价 三选二（回撤恶化至-34.2%，保留代码可复现）
DELAY_SELL = 1              # 实验否决：动能消失延迟成交（T+3 回撤-32.2%无增益，默认 T+1）
# ---- v7.11 估值/年线过滤（定稿） ----
VAL_GATE = True             # 估值剪刀差(股息率代理-10Y国债)分位门: ≥80%正常 / 50-80%半力(禁150档) / <50%超卖信号失效
MA250_GATE = True           # 年线门: 仅 价格<250日均线 时允许超卖信号（年线上方超卖=高位回调不执行）
WEEK_J0 = False             # 实验否决：周线共振门（wj<0 才允许超卖；"不动"/"半力"两档均降收益，保留代码可复现）
VAL_WIN = 3                 # 剪刀差滚动分位窗口（年；2y/3y/5y/expanding 实测：3y 收益-回撤平衡且无冷启动问题；5y 冷启动致2016-19段差）
CN10Y_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cn10y_daily.csv")
MAX_POS = 1.50
START = "2016-09-08"
LOOKBACK_DAYS = 4800      # 抓取回看（update.py 用；需覆盖 2014 起指标 warm-up）
SLIPPAGE_BPS = 5          # 单边滑点 5bp = 0.05%
FEE_RATE = 0.0001         # 万1
FEE_MIN = 5.0             # 最低 5 元
FIN_RATE = 0.07           # 融资年化 7%（>100% 杠杆部分，按交易日计息）
TRADING_DAYS = 252        # 年化基准
RISK_FREE = 0.0           # 夏普无风险利率


# ============ 数据 ============
def get_prices(tr_path, px_path, start=START, end=None):
    """读本地 CSV，合并价格指数与全收益指数（两边都有的日期），日期升序。
    CSV 自 2014 年起；start 只作回测起点标记，不截断（保留 START 前的指标 warm-up，
    与 v7.6 一致——KDJ/RSI/动量/布林都需要 2014-2016 的预热数据）。
    vol（H20269 成交量）保留用于量价背离。"""
    tr = pd.read_csv(tr_path, parse_dates=["date"])
    px = pd.read_csv(px_path, parse_dates=["date"])
    tr = tr[["date", "close"]].rename(columns={"close": "close"})
    if "trading_vol" in tr.columns:
        tr["vol"] = tr["trading_vol"]
    else:
        tr["vol"] = np.nan
    px = px[["date", "close"]].rename(columns={"close": "px"})
    df = tr.merge(px, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if end is not None:
        df = df[df["date"] <= pd.Timestamp(end)].reset_index(drop=True)
    return df


def build_signals(df, use_tr=False):
    """信号指标全部在价格指数 px 上计算；close 保留全收益用于收益核算。
    use_tr=True 时信号改用全收益序列（仅用于口径归因实验，主回测恒为 False）。"""
    c = df["close"] if use_tr else df["px"]
    df["ma20"] = c.rolling(20).mean()
    df["std20"] = c.rolling(20).std(ddof=0)
    df["upper"] = df["ma20"] + 2 * df["std20"]
    df["lower"] = df["ma20"] - 2 * df["std20"]
    df["ma200"] = c.rolling(200).mean()
    # 周线 KDJ(9,3,3)，ISO 周（跟随信号序列 c：默认 px，use_tr=True 时为 TR）
    iso = df["date"].dt.isocalendar()
    wk_key = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    wk = df.groupby(wk_key).agg(close=(c.name, "last"), date=("date", "last")).reset_index(drop=True)
    low9 = wk["close"].rolling(9).min()
    high9 = wk["close"].rolling(9).max()
    rsv = ((wk["close"] - low9) / (high9 - low9) * 100).fillna(50.0)
    k = np.empty(len(wk)); d = np.empty(len(wk)); k[0] = d[0] = 50.0
    for i in range(1, len(wk)):
        k[i] = 2 / 3 * k[i - 1] + 1 / 3 * rsv.iloc[i]
        d[i] = 2 / 3 * d[i - 1] + 1 / 3 * k[i]
    wk["J"] = 3 * k - 2 * d
    df["wj"] = df["date"].map(wk.set_index("date")["J"]).ffill()
    # 周线 RSI(14) Wilder（px）
    wk_dlt = wk["close"].diff()
    wg = wk_dlt.clip(lower=0).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    wl = (-wk_dlt).clip(lower=0).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    wk["RSI"] = (100 - 100 / (1 + wg / wl)).fillna(50.0)
    df["wrsi"] = df["date"].map(wk.set_index("date")["RSI"]).ffill()
    # 日线 RSI(14) Wilder（px，顶背离用）
    delta = c.diff()
    ru = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    rd = (-delta).clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + ru / rd)
    # 近63日动量（前一日窗口）
    df["lo63"] = c.rolling(63).min().shift(1)
    df["hi63"] = c.rolling(63).max().shift(1)
    df["up63"] = (c / df["lo63"] - 1) * 100
    df["dn63"] = (c / df["hi63"] - 1) * 100
    # 近10日动量（加速确认用）
    df["hi10"] = c.rolling(10).max().shift(1)
    df["dn10"] = (c / df["hi10"] - 1) * 100
    # ---- v7.11 估值剪刀差：股息率代理(TR/px 12个月滚动增长率) - 十年期国债收益率 ----
    # 代理股息率系统性偏高约0.4pp（含再投资收益），分位数形态与官方一致（2024-09 高企等），仅用于分位门
    if os.path.exists(CN10Y_CSV):
        y10 = pd.read_csv(CN10Y_CSV, parse_dates=["date"]).set_index("date")["y10"]
        df["div_proxy"] = (df["close"] / df["px"] / (df["close"].shift(252) / df["px"].shift(252)) - 1) * 100
        df["y10"] = df["date"].map(y10).ffill()
        df["spread"] = df["div_proxy"] - df["y10"]
        # v7.11 定稿：滚动分位窗口 VAL_WIN 年（2y/3y/5y/expanding 实测选 3y；5y 冷启动 min_periods>回测预热致早期信号全禁）
        win = int(VAL_WIN * 252)
        df["spread_pct"] = df["spread"].rolling(win, min_periods=int(win * 0.8)).rank(pct=True)
        df["os_half"] = df["spread_pct"].between(0.5, 0.8)
    else:
        df["spread"] = df["spread_pct"] = np.nan
        df["os_half"] = False
    # 超卖 2-of-4（px；v7.10 可选跌幅加速确认：dn63≤-Y_DOWN 且 近10日跌幅≥近63日跌幅×0.6）
    drop_cond = (df["dn63"] <= -Y_DOWN) & ((df["dn10"] <= df["dn63"] * 0.6) if Y_ACC else True)
    os_raw = ((df["wj"] < J_LOW).astype(int) + (c <= df["lower"]).astype(int)
              + drop_cond.astype(int) + (df["wrsi"] < RSI_OS).astype(int)) >= 2
    # v7.11 信号级过滤：估值分位<50% / 价格≥250日线 / 周线J≥0 时，超卖信号失效
    df["ma250"] = c.rolling(250).mean()
    if VAL_GATE:
        os_raw = os_raw & (df["spread_pct"] >= 0.5)
    if MA250_GATE:
        os_raw = os_raw & (c < df["ma250"])
    if WEEK_J0:
        os_raw = os_raw & (df["wj"] < 0)
    df["oversold"] = os_raw
    # A态超买三维极值（px）
    df["overbought"] = (df["wj"] > J_HIGH) & (c >= df["upper"]) & (df["up63"] >= X_UP)
    # 动能消失（px）
    df["hi10c"] = c.rolling(10).max().shift(1)
    df["hi10rsi"] = df["rsi"].rolling(10).max().shift(1)
    rsi_diverg = (c > df["hi10c"]) & (df["rsi"] < df["hi10rsi"])
    if DIV_2OF3:
        # 扩展顶背离：RSI + MACD柱 + 量价 三选二（v7.10 实验）
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        df["macd_hist"] = 2 * (dif - dea)
        df["hi10macd"] = df["macd_hist"].rolling(10).max().shift(1)
        macd_diverg = (c > df["hi10c"]) & (df["macd_hist"] < df["hi10macd"])
        vol = df["vol"].ffill()
        df["vol5"] = vol.rolling(5).mean()
        vol_diverg = (c > df["hi10c"]) & (df["vol"] < df["vol5"])
        df["diverg"] = (rsi_diverg.astype(int) + macd_diverg.astype(int) + vol_diverg.astype(int)) >= 2
    else:
        df["diverg"] = rsi_diverg
    df["j_cross"] = (df["wj"].rolling(10).max() > J_CROSS_FROM) & (df["wj"] <= J_CROSS_TO)
    df["rsi_cross"] = (df["wrsi"].rolling(10).max() > RSI_CROSS_FROM) & (df["wrsi"] <= RSI_CROSS_TO)
    df["momentum_lost"] = df["j_cross"] | df["rsi_cross"] | df["diverg"]
    return df


def replay(df, t1=True, start=START, delay_sell=DELAY_SELL):
    """T+1 撮合状态机重放（从 start 起输出；start 之前仅作信号预热）。
    信号 T 日收盘确认（用 df 上一行信号），T+1 日收盘成交（滑点计入成交价）。
    t1=False 时信号当日收盘确认、当日收盘成交（仅用于口径归因实验，主回测恒为 True）。
    delay_sell: 动能消失触发后第 N 个交易日成交（1=T+1 默认；3=延迟到第 3 交易日收盘成交，v7.10 实验）。
    返回 (trades, legs_closed, positions)：
      trades: 每笔 {date, action, px(信号价), fill(成交价含滑点), fee, slippage,
                    pos_before, pos_after, reason, amount}
      legs_closed: 抄底档闭环（FIFO）{buy_date, buy_fill, sell_date, sell_fill, reason, ret}
      positions: 每日目标仓位 Series（长度 = df 中 >= start 的行数）
    """
    trades = []
    legs = []          # [(成交索引, 成交日期, 成交价(含滑点))]
    legs_closed = []
    start_ts = pd.Timestamp(start)
    n_out = int((df["date"] >= start_ts).sum())
    positions = np.zeros(n_out)
    k = 0
    state, pos = "A", 1.0
    t0 = None          # B 态离场日
    prev = None        # T 日信号
    prev2 = None       # T-1 日信号（连续确认）
    pend = None        # 动能消失挂起: [确认行索引i, 确认日期] —— 5个交易日内 J 未回 85 才确认卖出
    for i in range(len(df)):
        r = df.iloc[i]
        d = r["date"]
        if d < start_ts:                # START 之前仅推进 prev/prev2（warm-up 信号），不参与撮合
            prev2, prev = prev, r
            continue
        osig = obsig = lost = False
        if t1:
            if prev is not None:                      # T 日收盘确认的信号
                osig = bool(prev["oversold"]); obsig = bool(prev["overbought"])
                lost = bool(prev["momentum_lost"])
        else:
            osig = bool(r["oversold"]); obsig = bool(r["overbought"]); lost = bool(r["momentum_lost"])
        act = None
        if state == "A":
            if osig: act = ("buy", 1.25, "C", "情绪极值超卖共振·加仓至125%")
            elif obsig: act = ("sell", 0.0, "B", "超买极值共振·清仓离场")
        elif state == "B":
            if osig: act = ("buy", 1.25, "C", "离场中现超卖共振·回补并加仓至125%")
            elif t0 is not None and d >= t0 + datetime.timedelta(days=REBUY_DAYS):
                act = ("buy", 1.0, "A", "离场满90自然日·强制回补至100%")
        elif state in ("C", "D"):
            # v7.11 估值"半力"：分位 50-80% 时禁止第二档加仓至 150%
            half = VAL_GATE and prev is not None and bool(prev["os_half"])
            if state == "C" and osig and pos < 1.5 and not half:
                act = ("buy", 1.5, "D", "再次超卖共振·加仓至150%")
            lost_now = prev is not None and bool(prev["momentum_lost"])
            if lost_now and pend is None:
                pend = [i - 1, d]          # 记录确认日 T（prev 行索引 i-1）
            if pend is not None and i - pend[0] >= delay_sell:
                act = ("sell", 1.0, "A", "动能消失·了结临时仓回100%")
                pend = None
            if act is None and legs and d >= legs[0][1] + datetime.timedelta(days=HOLD_DAYS):
                np_ = pos - 0.25
                act = ("sell", np_, "C" if np_ > 1.0 + 1e-9 else "A", "加仓满60自然日·卖出一档临时仓")
        # 期初建仓：回测起点首日直接 0 -> 100（无信号，T+1 框架下首日即持仓）
        if k == 0 and len(trades) == 0:
            act = ("buy", 1.0, "A", "期初建底仓·满仓100%")
        if act:
            new_pos, new_state = act[1], act[2]
            px_ = float(r["px"])
            fill = px_ * (1 + SLIPPAGE_BPS / 1e4) if act[0] == "buy" else px_ * (1 - SLIPPAGE_BPS / 1e4)
            trades.append({"date": d.strftime("%Y-%m-%d"), "action": "买入" if act[0] == "buy" else "卖出",
                           "px": round(px_, 2), "fill": round(fill, 2), "reason": act[3],
                           "pos_before": int(round(pos * 100)), "pos_after": int(round(new_pos * 100))})
            if act[0] == "buy" and new_pos > 1.0 + 1e-9 and new_pos > pos + 1e-9:
                legs.append((i, d, fill))
            if act[0] == "sell":
                n_legs = int(round((pos - new_pos) / 0.25))
                for _ in range(n_legs):
                    if legs:
                        b_i, b_d, b_f = legs.pop(0)
                        legs_closed.append({"buy_date": b_d.strftime("%Y-%m-%d"), "buy_fill": round(b_f, 2),
                                            "sell_date": d.strftime("%Y-%m-%d"), "sell_fill": round(fill, 2),
                                            "reason": act[3],
                                            "ret": round((fill - b_f) / b_f * 100, 2)})
            pos, state = new_pos, new_state
            if state == "A":
                t0 = None
            if state == "B":
                t0 = d
        positions[k] = pos
        k += 1
        prev2, prev = prev, r
    return (trades, legs_closed, positions, state, pos, legs, t0)


def equity_curve(df, trades, positions, initial=100000.0, start=START):
    """日频净值核算（全收益 close 计收益，T+1 撮合）。
    份额按信号日收盘价 px 成交（滑点+费用作为显式成本从净值扣除，数学等价）；
    融资成本在持仓日按杠杆部分（pos>1.0）按年化 7% / 252 交易日计提。
    df 须为 >= start 的回测段（positions 与之对齐）。
    返回 dict: dates/strategy_nav/bh_nav/strategy_dd/bh_dd/pos_pct/costs。"""
    dates = df["date"].dt.strftime("%Y-%m-%d").tolist()
    px = df["px"].values
    n = len(df)
    cash = initial
    shares = 0.0
    nav_ts = np.zeros(n)
    costs_ts = np.zeros(n)
    pos_pct = np.zeros(n)
    t_by_date = {t["date"]: t for t in trades}
    s = SLIPPAGE_BPS / 1e4
    for i in range(n):
        dstr = dates[i]
        px_close = px[i]
        t = t_by_date.get(dstr)
        if t is not None:
            if t["action"] == "买入":
                cur_val = cash + shares * px_close
                target_val = cur_val * (t["pos_after"] / 100.0)
                buy_amt = max(0.0, target_val - shares * px_close)
                if buy_amt > 0:
                    fee = max(buy_amt * FEE_RATE, FEE_MIN)
                    slip = buy_amt * s
                    shares += buy_amt / px_close
                    cash -= buy_amt
                    costs_ts[i] += fee + slip
            else:
                cur_val = cash + shares * px_close
                target_val = cur_val * (t["pos_after"] / 100.0)
                sell_amt = max(0.0, shares * px_close - target_val)
                if sell_amt > 0:
                    fee = max(sell_amt * FEE_RATE, FEE_MIN)
                    slip = sell_amt * s
                    shares -= sell_amt / px_close
                    cash += sell_amt
                    costs_ts[i] += fee + slip
        # 融资成本（持仓日计提）：杠杆部分按日计息
        p = positions[i]
        if p > 1.0 + 1e-9:
            val = cash + shares * px_close
            costs_ts[i] += val * (p - 1.0) * FIN_RATE / TRADING_DAYS
        pos_pct[i] = p * 100
        nav_ts[i] = cash + shares * px_close
    # 显式成本在净值中扣除（等价于每日从收益扣减）
    cum_cost = np.cumsum(costs_ts)
    strategy_nav = (nav_ts - cum_cost) / initial
    bh_nav = df["close"].values / df["close"].iloc[0]
    dd_s = strategy_nav / np.maximum.accumulate(strategy_nav) - 1
    dd_b = bh_nav / np.maximum.accumulate(bh_nav) - 1
    return {"dates": dates, "strategy_nav": strategy_nav, "bh_nav": bh_nav,
            "strategy_dd": dd_s, "bh_dd": dd_b, "pos_pct": pos_pct, "costs": costs_ts}


def metrics(ec, trades, initial=100000.0):
    """年化/夏普/CAGR/最大回撤/费用/滑点/利息，全部在日频序列上计算。"""
    nav = ec["strategy_nav"]
    bh = ec["bh_nav"]
    n = len(nav)
    years = n / TRADING_DAYS
    total = nav[-1] - 1
    cagr = nav[-1] ** (1 / years) - 1
    rets = np.diff(nav) / nav[:-1]
    sharpe = (rets.mean() / rets.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(rets) > 1 and rets.std(ddof=1) > 0 else 0.0
    total_bh = bh[-1] - 1
    cagr_bh = bh[-1] ** (1 / years) - 1
    rets_bh = np.diff(bh) / bh[:-1]
    sharpe_bh = (rets_bh.mean() / rets_bh.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(rets_bh) > 1 and rets_bh.std(ddof=1) > 0 else 0.0
    fee = sum(t.get("fee", 0) for t in trades)
    return {"total": float(total), "ann": float(cagr), "sharpe": float(sharpe),
            "mdd": float(dd_min(ec["strategy_dd"])), "n_trades": len(trades),
            "total_fee": round(float(ec["costs"].sum()), 2),
            "total_bh": float(total_bh), "ann_bh": float(cagr_bh), "sharpe_bh": float(sharpe_bh),
            "mdd_bh": float(dd_min(ec["bh_dd"])),
            "final_value": float(nav[-1] * initial), "final_bh": float(bh[-1] * initial),
            "fee_total": round(float(ec["costs"].sum()), 2)}


def dd_min(dd_series):
    return float(np.min(dd_series)) if len(dd_series) else 0.0


def oversold_stats(closed):
    """抄底胜率（FIFO 闭环，按买入年份/卖出原因分组）。"""
    st = {"total": len(closed), "wins": sum(1 for c in closed if c["ret"] > 0),
          "losses": sum(1 for c in closed if c["ret"] <= 0), "closed": closed}
    if not closed:
        st["winrate"], st["avg_ret"] = 0.0, 0.0
        st["yearly"], st["reason"] = [], []
        return st
    st["winrate"] = round(st["wins"] / len(closed) * 100, 1)
    st["avg_ret"] = round(sum(c["ret"] for c in closed) / len(closed), 2)
    by_year, by_reason = {}, {}
    for c in closed:
        by_year.setdefault(c["buy_date"][:4], []).append(c)
        key = "到期强制卖出" if "到期" in c["reason"] else ("动能消失·顶背离" if "顶背离" in c["reason"]
                              else ("动能消失·J跌破80" if "J跌破80" in c["reason"]
                                    else ("动能消失·RSI跌破65" if "RSI跌破65" in c["reason"] else c["reason"])))
        by_reason.setdefault(key, []).append(c)
    st["yearly"] = []
    for y in sorted(by_year):
        g = by_year[y]; n = len(g); w = sum(1 for c in g if c["ret"] > 0)
        st["yearly"].append({"year": y, "n": n, "wins": w, "losses": n - w,
                             "winrate": round(w / n * 100, 1),
                             "avg_ret": round(sum(c["ret"] for c in g) / n, 2), "closed": g})
    st["reason"] = []
    for k in by_reason:
        g = by_reason[k]; n = len(g); w = sum(1 for c in g if c["ret"] > 0)
        st["reason"].append({"reason": k, "n": n, "wins": w, "losses": n - w,
                             "winrate": round(w / n * 100, 1),
                             "avg_ret": round(sum(c["ret"] for c in g) / n, 2)})
    st["reason"].sort(key=lambda x: -x["n"])
    return st


def overview_stats(trades, closed, df):
    """策略概览（给用户全貌与预期）：满仓+超跌抄底的操作频率统计。
    返回 dict: years / add_total / add_per_year / clear_total / clear_per_year /
               temp_sell_per_year / avg_hold_days / by_year / pct125 / pct150 /
               os_total / os_winrate / os_avg_ret / first / last"""
    n_days = (df["date"].iloc[-1] - df["date"].iloc[0]).days
    years = max(n_days / 365.25, 1e-9)
    add = [t for t in trades if t["action"] == "买入" and t["pos_after"] > 100 and "期初" not in t["reason"]]
    clear = [t for t in trades if t["action"] == "卖出" and t["pos_after"] == 0]
    temp_sell = [t for t in trades if t["action"] == "卖出" and t["pos_after"] == 100 and t["pos_before"] > 100]
    by_year = {}
    for t in add:
        by_year[t["date"][:4]] = by_year.get(t["date"][:4], 0) + 1
    hold_days = []
    for c in closed:
        b = pd.Timestamp(c["buy_date"]); s = pd.Timestamp(c["sell_date"])
        hold_days.append((s - b).days)
    n125 = sum(1 for t in add if t["pos_after"] == 125)
    n150 = sum(1 for t in add if t["pos_after"] == 150)
    wins = sum(1 for c in closed if c["ret"] > 0)
    return {
        "years": round(years, 1),
        "add_total": len(add),
        "add_per_year": round(len(add) / years, 1),
        "clear_total": len(clear),
        "clear_per_year": round(len(clear) / years, 2),
        "temp_sell_per_year": round(len(temp_sell) / years, 1),
        "avg_hold_days": round(sum(hold_days) / len(hold_days), 0) if hold_days else 0,
        "hold_min": min(hold_days) if hold_days else 0,
        "hold_max": max(hold_days) if hold_days else 0,
        "by_year": by_year,
        "pct125": n125, "pct150": n150,
        "os_total": len(closed),
        "os_winrate": round(wins / len(closed) * 100, 1) if closed else 0.0,
        "os_avg_ret": round(sum(c["ret"] for c in closed) / len(closed), 2) if closed else 0.0,
        "first": df["date"].iloc[0].strftime("%Y-%m-%d"),
        "last": df["date"].iloc[-1].strftime("%Y-%m-%d"),
    }


def run(tr_path, px_path, start=START, end=None, delay_sell=DELAY_SELL):
    """完整回测入口：读数据(含 warm-up) -> 信号 -> 撮合 -> 净值 -> 指标。
    df 保留 START 前数据作指标预热；回测与净值核算从 start 起。"""
    df_all = get_prices(tr_path, px_path, end=end)
    df_all = build_signals(df_all)
    trades, closed, positions, state, pos, legs, t0 = replay(df_all, t1=True, start=start,
                                                             delay_sell=delay_sell)
    df = df_all[df_all["date"] >= pd.Timestamp(start)].reset_index(drop=True)
    ec = equity_curve(df, trades, positions, start=start)
    m = metrics(ec, trades)
    m["n_oversold"] = len(closed)
    return {"df": df, "df_all": df_all, "trades": trades, "closed": closed, "positions": positions,
            "ec": ec, "metrics": m, "os_stat": oversold_stats(closed),
            "overview": overview_stats(trades, closed, df),
            "state": state, "pos": pos, "legs": legs, "t0": t0}


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    out = sys.argv[sys.argv.index("--out-dir") + 1] if "--out-dir" in sys.argv else base
    start = sys.argv[sys.argv.index("--start") + 1] if "--start" in sys.argv else START
    r = run(os.path.join(base, "h20269_daily.csv"), os.path.join(base, "h30269_daily.csv"), start)
    m = r["metrics"]
    print(f"交易日 {len(r['df'])} | 区间 {r['df']['date'].iloc[0].date()} ~ {r['df']['date'].iloc[-1].date()}")
    print(f"策略: 总收益 {m['total']*100:+.1f}% | 年化 {m['ann']*100:.2f}% | 夏普 {m['sharpe']:.3f} | 最大回撤 {m['mdd']*100:.1f}%")
    print(f"买入持有: 总收益 {m['total_bh']*100:+.1f}% | 年化 {m['ann_bh']*100:.2f}% | 夏普 {m['sharpe_bh']:.3f} | 回撤 {m['mdd_bh']*100:.1f}%")
    print(f"交易 {m['n_trades']} 笔 | 总成本(费+滑点+利息) {m['fee_total']:.2f} 元 | 终值 {m['final_value']:,.0f} / BH {m['final_bh']:,.0f}")
    st = r["os_stat"]
    print(f"抄底闭环 {st['total']} 档 胜率 {st['winrate']}% ({st['wins']}盈/{st['losses']}亏) 平均 {st['avg_ret']}%")
    # 写产物
    import pandas as pd
    ec = r["ec"]
    pd.DataFrame({"date": ec["dates"], "strategy_nav": ec["strategy_nav"], "bh_nav": ec["bh_nav"],
                  "strategy_dd": ec["strategy_dd"], "bh_dd": ec["bh_dd"],
                  "pos_pct": ec["pos_pct"], "costs": ec["costs"]}).to_csv(
        os.path.join(out, "equity_curve_v77.csv"), index=False)
    pd.DataFrame(r["trades"]).to_csv(os.path.join(out, "trades_v77.csv"), index=False)
    json.dump({k: (float(v) if isinstance(v, (int, float, np.floating)) else v) for k, v in m.items()},
              open(os.path.join(out, "metrics_v77.json"), "w"), indent=2, ensure_ascii=False)
    print(f"产物已写入 {out}/ (equity_curve_v77.csv / trades_v77.csv / metrics_v77.json)")
