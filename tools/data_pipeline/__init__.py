"""quant-hub 数据链路编排（tools/data_pipeline）。

这些脚本由 quant-hub-data 的数据 workflow 调用（workflow 检出本代码仓、把数据仓挂在 data/），
也可本地跑。全部只依赖 common/ 的冻结 API，不重复造轮子：

  index.py  —— 大盘/指数：ifzq 日K（分页回溯全史）→ 逐月封存 → aggregate 周/月K → 新鲜度门禁
  （stock.py / etf.py 见各自模块）

设计原则（方案 §2 / §4.2）：
  * 抓取与聚合放在**同一次运行的同一序列**里，不给"daily 更新了但 weekly 没重算"留窗口（R17）
  * 任何丢弃/降级都计数并落 runlog，禁止静默（§9.9）
  * 门禁红 = 中止且不落盘（宁缺勿错）
"""
