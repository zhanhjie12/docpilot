# 部署骨架

后续添加 Dockerfile、docker-compose.yml、Nginx 配置、迁移脚本与 CI。开发期可使用 SQLite
和本地文件目录；演示/生产期建议 PostgreSQL、对象存储、独立 Worker 和受控向量索引。

部署时必须通过环境变量注入密钥，限制上传大小，配置 HTTPS、CORS、备份、日志脱敏和健康检查。
