#!/usr/bin/env python3
"""增量拉取全 A 股日线前复权收盘价（腾讯财经API），输出 data/prices.json
增量策略: 读取已有 prices.json，每只股票只拉取从最后交易日到今天的数据
用法: python3 scripts/fetch_prices.py [--limit N] [--workers N]
"""
import json, os, sys, time, urllib.request, argparse, random
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
STOCKS_FILE = os.path.join(DATA_DIR, "stocks.json")
PRICES_FILE = os.path.join(DATA_DIR, "prices.json")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

def market_prefix(code):
    """沪市 sh, 深市 sz, 北交所 bj"""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith(("8", "4")):
        return "bj"
    return "sz"

def fetch_klines(code, lmt=80):
    """拉取最近 lmt 个交易日K线，返回 [(date, close), ...]"""
    prefix = market_prefix(code)
    symbol = f"{prefix}{code}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{lmt},qfq"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    stock_data = d.get("data", {}).get(symbol, {})
    klines = stock_data.get("qfqday") or stock_data.get("day") or []
    result = []
    for k in klines:
        if len(k) >= 3:
            try:
                result.append((k[0], float(k[2])))  # date, close
            except (ValueError, IndexError):
                continue
    return result

def update_one(code, existing, max_lookback=80, max_retries=2):
    """更新单只股票，返回 (code, new_data_or_None)"""
    old = existing.get(code, {"dates": [], "closes": []})
    last_date = old["dates"][-1] if old["dates"] else None

    for attempt in range(max_retries + 1):
        try:
            klines = fetch_klines(code, lmt=max_lookback)
            if not klines:
                return code, None
            if last_date:
                new = [(d, c) for d, c in klines if d > last_date]
                if not new:
                    return code, None
                dates = old["dates"] + [d for d, _ in new]
                closes = old["closes"] + [c for _, c in new]
            else:
                dates = [d for d, _ in klines]
                closes = [c for _, c in klines]
            return code, {"dates": dates, "closes": closes}
        except Exception as e:
            if attempt < max_retries:
                time.sleep(0.3 + attempt * 0.5 + random.random() * 0.2)
            else:
                return code, None

def run_batch(codes, existing, workers, max_retries, label="", batch_sleep=5):
    """批量拉取，每500只休息batch_sleep秒，返回 (results dict, failed_codes list)"""
    results = {}
    failed = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(update_one, code, existing, 80, max_retries): code for code in codes}
        for i, fut in enumerate(as_completed(futures), 1):
            code, data = fut.result()
            if data:
                results[code] = data
            else:
                failed.append(code)
            if i % 500 == 0:
                print(f"  {label}进度 {i}/{len(codes)}, 成功 {len(results)}, 失败 {len(failed)}，休息{batch_sleep}秒", flush=True)
                time.sleep(batch_sleep)
    return results, failed

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只处理前N只（调试用）")
    parser.add_argument("--workers", type=int, default=5, help="并发数（默认5）")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STOCKS_FILE, encoding="utf-8") as f:
        stocks = json.load(f)
    if args.limit:
        stocks = stocks[:args.limit]
    all_codes = [code for code, _ in stocks]
    print(f"待更新股票: {len(all_codes)} 只, 并发: {args.workers}", flush=True)

    existing = {}
    if os.path.exists(PRICES_FILE):
        with open(PRICES_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        print(f"已有价格数据: {len(existing)} 只", flush=True)

    # 第一轮：5并发，每500只休息8秒
    print(f"\n=== 第一轮：{args.workers}并发拉取（每500只休息8秒）===", flush=True)
    results, failed = run_batch(all_codes, existing, args.workers, 0, "第一轮", batch_sleep=8)
    print(f"第一轮完成: 成功 {len(results)}, 失败 {len(failed)}", flush=True)

    # 第二轮：3并发重试
    if failed:
        print(f"\n=== 第二轮：3并发重试 {len(failed)} 只 ===", flush=True)
        time.sleep(5)
        retry_results, still_failed = run_batch(failed, existing, 3, 3, "第二轮", batch_sleep=3)
        results.update(retry_results)
        print(f"第二轮完成: 额外成功 {len(retry_results)}, 仍失败 {len(still_failed)}", flush=True)
        failed = still_failed

    # 合并已有数据
    for code in existing:
        if code not in results:
            results[code] = existing[code]

    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)
    print(f"\n完成: 总计 {len(results)} 只有价格数据, 最终失败 {len(failed)} 只", flush=True)
    if failed:
        print(f"失败股票(前20): {failed[:20]}", flush=True)

if __name__ == "__main__":
    main()
