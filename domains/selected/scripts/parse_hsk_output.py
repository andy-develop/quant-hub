#!/usr/bin/env python3
"""解析 hsk-cli +host 的 JSON 输出，提取 resource_id/verify_code/public_url
用法: python3 scripts/parse_hsk_output.py <hsk_output.json>
输出: RESOURCE_ID=xxx, VERIFY_CODE=xxx, PUBLIC_URL=xxx
"""
import json, re, sys

def main():
    if len(sys.argv) < 2:
        print("用法: parse_hsk_output.py <json_file>")
        sys.exit(1)
    raw = open(sys.argv[1], encoding="utf-8").read()
    # 尝试直接解析
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        # 提取 stdout 字段中的 JSON
        m = re.search(r'"stdout":\s*"(.*?)"\s*[,}]', raw, re.DOTALL)
        if not m:
            print("错误: 无法解析输出")
            sys.exit(1)
        inner = m.group(1).encode().decode("unicode_escape")
        d = json.loads(inner)
    data = d.get("data", d)
    rid = data.get("resource_id", "")
    code = data.get("verify_code", "")
    url = data.get("public_url", "")
    # 去掉 URL 中的 verify_code 参数
    if "?" in url:
        url = url.split("?")[0]
    print(f"RESOURCE_ID={rid}")
    print(f"VERIFY_CODE={code}")
    print(f"PUBLIC_URL={url}")

if __name__ == "__main__":
    main()
