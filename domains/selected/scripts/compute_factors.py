#!/usr/bin/env python3
"""基于 prices.json 计算技术因子，输出 data/factors.json
计算维度: 20日动量、60日动量、20日年化波动率
用法: python3 scripts/compute_factors.py
"""
import json, os, math, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
PRICES_FILE = os.path.join(DATA_DIR, "prices.json")
FACTORS_FILE = os.path.join(DATA_DIR, "factors.json")

def pct_change(closes):
    """日收益率序列"""
    return [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))]

def calc_momentum(closes, window):
    """N日动量 = close[-1] / close[-1-window] - 1"""
    if len(closes) <= window:
        return None
    return closes[-1] / closes[-1 - window] - 1

def calc_volatility(closes, window=20):
    """N日年化波动率 = std(日收益) * sqrt(252)"""
    if len(closes) <= window:
        return None
    rets = pct_change(closes[-window - 1:])
    if not rets:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * math.sqrt(252)

def main():
    if not os.path.exists(PRICES_FILE):
        print(f"错误: {PRICES_FILE} 不存在，请先运行 fetch_prices.py")
        sys.exit(1)
    with open(PRICES_FILE, encoding="utf-8") as f:
        prices = json.load(f)
    print(f"读取价格数据: {len(prices)} 只")

    factors = {}
    skipped = 0
    for code, data in prices.items():
        closes = data.get("closes", [])
        if len(closes) < 21:
            skipped += 1
            continue
        mom_20 = calc_momentum(closes, 20)
        mom_60 = calc_momentum(closes, 60)
        vol_20 = calc_volatility(closes, 20)
        if mom_20 is None or vol_20 is None:
            skipped += 1
            continue
        factors[code] = {
            "close": round(closes[-1], 2),
            "mom_20": round(mom_20, 4),
            "mom_60": round(mom_60, 4) if mom_60 is not None else None,
            "vol_20": round(vol_20, 4),
        }
    with open(FACTORS_FILE, "w", encoding="utf-8") as f:
        json.dump(factors, f, ensure_ascii=False)
    print(f"完成: {len(factors)} 只有效因子, 跳过 {skipped} 只（数据不足）")
    print(f"输出: {FACTORS_FILE}")
    # 打印统计
    if factors:
        mom20s = [v["mom_20"] for v in factors.values()]
        vols = [v["vol_20"] for v in factors.values()]
        print(f"  20日动量: 均值 {sum(mom20s)/len(mom20s):.2%}, 中位 {sorted(mom20s)[len(mom20s)//2]:.2%}")
        print(f"  20日波动率: 均值 {sum(vols)/len(vols):.2%}, 中位 {sorted(vols)[len(vols)//2]:.2%}")

if __name__ == "__main__":
    main()
