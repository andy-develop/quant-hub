"""pytest 根路径保险：无论以何种方式调用 pytest，仓库根都在 sys.path。

（`python -m pytest` 自带 CWD；裸 `pytest` 不带 —— CI 首跑就栽在这。）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ★ 不写 .pyc：测试要读真实源码给结论，而 .pyc 的失效判据是 `(mtime 秒, size)`。
#   等长改动（例如交换两个调用顺序）落在同一秒内时，陈旧 .pyc 会被照旧使用 ——
#   实测「错误顺序仍 62 passed」，是货真价实的假绿。CI 是新检出所以碰不到，
#   本地却会骗人，所以直接不生成。
sys.dont_write_bytecode = True
