"""后台解析与索引任务。

后续实现任务队列消费者：拉取待处理版本 -> 解析 -> Chunk -> 索引 -> 更新状态；
支持幂等、重试、进度事件、失败原因和取消任务。MVP 可用 FastAPI BackgroundTasks，
生产环境再替换 Celery/RQ/Arq 等队列。
"""
