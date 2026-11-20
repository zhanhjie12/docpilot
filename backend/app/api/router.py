"""汇总业务路由；由 main.py 统一挂载到 /api/v1。"""

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, require_authenticated
from app.api.routes import chat, documents, policies, reviews, screening, tasks, tenders

# 业务路由统一注入请求追踪和可配置的认证检查；开发环境可保持匿名访问。
api_router = APIRouter(dependencies=[Depends(get_request_id), Depends(require_authenticated)])

# 静态筛选路径优先注册，文档动态路径放在后面。
api_router.include_router(screening.router, prefix="/documents", tags=["screening"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(reviews.router, prefix="/reviews", tags=["reviews"])
api_router.include_router(policies.router, prefix="/policies", tags=["policies"])
api_router.include_router(tenders.router, prefix="/tenders", tags=["tenders"])
