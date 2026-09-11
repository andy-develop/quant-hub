"""quant-hub 数据链路编排（tools/data_pipeline）。

这些脚本由 quant-hub-data 的数据 workflow 调用（workflow 检出本代码仓、把数据仓挂在 data/），
也可本地跑。全部只依赖 common/ 的冻结 API，不重复造轮子：

  index.py  —— 大盘/指数：ifzq 日K（分页回溯全史）→ 逐月封存 → aggregate 周/月K → 新鲜度门禁
  verify.py —— 数据仓自检：契约/不变量/日历/派生一致性/manifest
  （stock.py / etf.py 见各自模块）

设计原则（方案 §2 / §4.2）：
  * 抓取与聚合放在**同一次运行的同一序列**里，不给"daily 更新了但 weekly 没重算"留窗口（R17）
  * 任何丢弃/降级都计数并落 runlog，禁止静默（§9.9）
  * 门禁红 = 中止且不落盘（宁缺勿错）
"""
from __future__ import annotations

import os


def prune_manifest(root: str, asset: str, fq: str) -> int:
    """删除 manifest 中指向已不存在路径的分区条目，返回删掉的条数。

    为什么需要：`seal_partition` 把某月日分片合并进 year=/month= 后会**删掉** _incr 分片，
    但 `write_incremental` 早先写进 manifest 的 _incr 条目不会被自动清理 -> 留下指向
    已删除文件的僵尸条目。封存后调用本函数即可让 manifest 与磁盘一致（verify.py 据此判干净）。
    只用 writer 的公共 read_manifest/write_manifest，不改冻结的 writer 语义。
    """
    from common.store.writer import read_manifest, write_manifest
    man = read_manifest(root, asset, fq)
    parts = man.get("partitions", []) or []
    keep = []
    dropped = 0
    for p in parts:
        rel = p.get("path")
        if not rel:
            keep.append(p)
            continue
        full = os.path.join(root, rel)
        if os.path.exists(full):        # 文件或目录皆可
            keep.append(p)
        else:
            dropped += 1
    if dropped:
        man["partitions"] = keep
        write_manifest(root, asset, fq, man)
    return dropped
