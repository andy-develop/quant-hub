#!/usr/bin/env python3
"""读取模板 + 数据，生成最终 index.html
用法: python3 scripts/build_html.py
输出: index.html
"""
import json, os, sys, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
TEMPLATE = os.path.join(ROOT, "templates", "index_template.html")
OUTPUT = os.path.join(ROOT, "index.html")

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default

def js_array_str(arr):
    """Python list -> JS 数组字符串（紧凑格式）"""
    return json.dumps(arr, ensure_ascii=False, separators=(",", ":"))

def main():
    # 读取数据
    stocks = load_json(os.path.join(DATA_DIR, "stocks.json"), [])
    factors = load_json(os.path.join(DATA_DIR, "factors.json"), {})
    print(f"股票: {len(stocks)} 只, 因子: {len(factors)} 只")

    if not stocks:
        print("警告: stocks.json 为空，将使用空股票池")

    # 生成时间
    now = datetime.datetime.now()
    gen_time = now.strftime("%H:%M")
    data_date = now.strftime("%Y-%m-%d")

    # 读取模板
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()

    # 替换占位符
    html = html.replace("/*__STOCK_UNIVERSE__*/[]", js_array_str(stocks))
    html = html.replace("/*__REAL_FACTORS__*/{}", json.dumps(factors, ensure_ascii=False, separators=(",", ":")))
    html = html.replace("/*__GEN_TIME__*/", gen_time)
    html = html.replace("/*__DATA_DATE__*/", data_date)

    # 写入
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(OUTPUT) / 1024
    print(f"生成完成: {OUTPUT} ({size_kb:.1f} KB)")

if __name__ == "__main__":
    main()
