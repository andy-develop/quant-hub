"""common —— 三域共用的底层能力。

只放三域**已经各自实现过一遍**的东西（限流退避、交易日历、payload carry-forward、
幂等断言、发布自检），不发明新抽象。

依赖约束（硬）：`common/requirements.txt` **只允许 `pyarrow requests`**，禁 pandas/numpy
—— 否则它自己就成了版本冲突的新源头（方案 §4.4）。

其中：
- `common/calendar.py` 纯标准库，无任何第三方依赖
- `common/store/schema.py` 纯标准库
- `common/store/reader.py` / `writer.py` 需要 pandas（延迟导入，契约测试在无 pandas 时仍可跑）
- `common/aggregate.py` 需要 pandas（延迟导入）
"""

from __future__ import annotations

__all__ = ["calendar"]
