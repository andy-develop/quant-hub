"""本地部署：定时拉取（fetch.py）+ 数据服务（serve.py）。

架构：parquet 文件是唯一真相（复用 common/store + tools/data_pipeline），
内存只是数据服务内的查询缓存，不是第二存储引擎。
"""
