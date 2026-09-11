"""pytest 配置：把 selected 域的 scripts/ 加入 import 路径。

个性化选股域（原 stock-factor-engine）脚本之间不做包内 import，各自以
`ROOT = dirname(dirname(__file__))` 定位 data/。测试需要直接 import
`compute_factors` 等模块来验证纯函数，故在此把 scripts/ 挂上路径。

只在单独跑本域时生效：`pytest domains/selected/tests`。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "scripts"))
