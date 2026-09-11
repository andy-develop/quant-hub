#!/usr/bin/env python3
"""拉取全 A 股股票列表，输出 data/stocks.json
用法: python3 scripts/fetch_stocks.py
"""
import json, os, sys, time, urllib.request

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUT = os.path.join(DATA_DIR, "stocks.json")

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# 备用节点
NODES = [
    "https://push2.eastmoney.com",
    "https://1.push2.eastmoney.com",
    "https://2.push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "http://push2.eastmoney.com",
]

# 全板块：深市主板、创业板、北交所、沪市主板、科创板
FS = "m:0+t:6,m:0+t:80,m:0+t:81,m:1+t:2,m:1+t:23"

def fetch_page(pn, pz=100):
    last_err = None
    for node in NODES:
        url = (f"{node}/api/qt/clist/get?"
               f"pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f3&"
               f"fs={FS}&fields=f12,f14")
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8"))
                if d.get("data"):
                    return d
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise last_err if last_err else Exception("all nodes failed")

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    # 先探总数
    for attempt in range(5):
        try:
            first = fetch_page(1, 1)
            total = first.get("data", {}).get("total", 0)
            break
        except Exception as e:
            print(f"探总数失败({attempt+1}/5): {e}")
            time.sleep(3)
    else:
        print("错误: 无法获取股票总数")
        sys.exit(1)

    print(f"全 A 股总数: {total}")
    pz = 100
    pages = (total + pz - 1) // pz
    stocks = []
    for pn in range(1, pages + 1):
        for attempt in range(5):
            try:
                d = fetch_page(pn, pz)
                items = d.get("data", {}).get("diff", []) or []
                for it in items:
                    code = it.get("f12", "")
                    name = it.get("f14", "")
                    if code and name:
                        stocks.append([code, name])
                if pn % 5 == 0 or pn == pages:
                    print(f"  第 {pn}/{pages} 页，累计 {len(stocks)} 只")
                break
            except Exception as e:
                print(f"  第 {pn} 页失败({attempt+1}/5): {e}")
                time.sleep(2)
        time.sleep(0.2)
    # 去重
    seen = set()
    uniq = []
    for c, n in stocks:
        if c not in seen:
            seen.add(c)
            uniq.append([c, n])
    uniq.sort(key=lambda x: x[0])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(uniq, f, ensure_ascii=False)
    print(f"完成: {len(uniq)} 只 -> {OUT}")
    if len(uniq) < 4000:
        print(f"警告: 股票数量偏少({len(uniq)})，可能API受限")

if __name__ == "__main__":
    main()
