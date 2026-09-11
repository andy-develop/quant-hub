"""pytest 根路径保险：无论以何种方式调用 pytest，仓库根都在 sys.path。

（`python -m pytest` 自带 CWD；裸 `pytest` 不带 —— CI 首跑就栽在这。）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
