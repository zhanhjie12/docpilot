"""用户注册、登录和当前用户查询接口。"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import get_current_user, get_database
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import create_access_token, hash_password, verify_password
from app.domain.schemas import AuthResponse, LoginRequest, RegisterRequest, UserResponse
from app.infrastructure.database import SQLiteDatabase, utc_now


router = APIRouter()


def _public_user(row: dict[str, object]) -> dict[str, object]:
    """只返回用户公开字段，绝不把 password_hash 传给前端。"""

    return {
        "user_id": row["user_id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row.get("role", "user"),
        "created_at": row["created_at"],
    }


def _auth_response(row: dict[str, object], settings: Settings) -> dict[str, object]:
    """为用户签发访问令牌，并返回前端需要的统一认证响应。"""

    expires_in = settings.access_token_expire_minutes * 60
    token = create_access_token(
        user_id=str(row["user_id"]),
        email=str(row["email"]),
        role=str(row.get("role", "user")),
        secret=settings.jwt_secret,
        expires_in_seconds=expires_in,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "user": _public_user(row),
    }


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED, summary="注册用户")
async def register(payload: RegisterRequest, request: Request, db: SQLiteDatabase = Depends(get_database)):
    """创建用户、保存密码哈希，并立即返回登录令牌。"""

    now = utc_now()
    user_id = f"user_{uuid4().hex}"
    try:
        db.run(
            "INSERT INTO users(user_id,email,password_hash,display_name,role,is_active,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                user_id,
                payload.email,
                hash_password(payload.password),
                payload.display_name,
                "user",
                1,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        # 邮箱是唯一键，重复注册返回稳定的业务错误而不是数据库堆栈。
        raise AppError(
            code="EMAIL_ALREADY_REGISTERED",
            message="该邮箱已经注册",
            status_code=409,
            retryable=False,
        ) from exc
    row = db.one(
        "SELECT user_id,email,display_name,role,created_at,is_active FROM users WHERE user_id=?",
        (user_id,),
    )
    if row is None:  # pragma: no cover - guarded by the successful INSERT above
        raise AppError(code="USER_CREATE_FAILED", message="用户创建失败", status_code=500)
    return _auth_response(row, request.app.state.settings)


@router.post("/login", response_model=AuthResponse, summary="用户登录")
async def login(payload: LoginRequest, request: Request, db: SQLiteDatabase = Depends(get_database)):
    """校验密码并签发短期 Bearer 访问令牌。"""

    row = db.one(
        "SELECT user_id,email,password_hash,display_name,role,created_at,is_active "
        "FROM users WHERE email=?",
        (payload.email,),
    )
    if not row or not row.get("is_active") or not verify_password(payload.password, str(row["password_hash"])):
        # 不区分“邮箱不存在”和“密码错误”，避免泄露账号是否注册。
        raise AppError(
            code="INVALID_CREDENTIALS",
            message="邮箱或密码错误",
            status_code=401,
            retryable=False,
        )
    db.run("UPDATE users SET last_login_at=?,updated_at=? WHERE user_id=?", (utc_now(), utc_now(), row["user_id"]))
    return _auth_response(row, request.app.state.settings)


@router.get("/me", response_model=UserResponse, summary="查询当前用户")
async def current_user(user: dict[str, object] = Depends(get_current_user)):
    """验证令牌并返回当前用户公开资料。"""

    return _public_user(user)
