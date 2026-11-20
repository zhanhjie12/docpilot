"""密码哈希和 Bearer 访问令牌工具。

认证模块只使用 Python 标准库：密码采用带随机盐的 PBKDF2，访问令牌采用
HMAC-SHA256 签名的紧凑 JSON 格式。这样本地 MVP 不需要额外安装 JWT 库，
同时保留了后续替换为成熟身份服务的边界。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 310_000
TOKEN_ALGORITHM = "HS256"


class AuthenticationError(ValueError):
    """令牌或密码校验失败，不暴露具体失败原因。"""


def _b64encode(value: bytes) -> str:
    """使用 URL 安全编码，避免令牌出现在 Header 时包含特殊字符。"""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    """恢复无填充 Base64，并统一把非法输入转换为认证错误。"""

    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise AuthenticationError("invalid authentication token") from exc


def hash_password(password: str) -> str:
    """对明文密码进行带随机盐的 PBKDF2 哈希，返回可持久化字符串。"""

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(PASSWORD_ITERATIONS),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """校验密码哈希，使用 compare_digest 避免简单的时序泄露。"""

    try:
        scheme, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(iterations_text)
        if iterations <= 0:
            return False
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(actual, expected)
    except (AuthenticationError, TypeError, ValueError):
        return False


def create_access_token(
    *,
    user_id: str,
    email: str,
    role: str,
    secret: str,
    expires_in_seconds: int,
) -> str:
    """创建包含用户身份和过期时间的签名访问令牌。"""

    now = int(time.time())
    header = {"alg": TOKEN_ALGORITHM, "typ": "JWT"}
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + expires_in_seconds,
        "jti": secrets.token_hex(12),
    }
    encoded_header = _b64encode(json.dumps(header, separators=(",", ":")).encode())
    encoded_payload = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64encode(signature)}"


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    """验证签名和过期时间，并返回令牌载荷。"""

    parts = token.split(".")
    if len(parts) != 3:
        raise AuthenticationError("invalid authentication token")
    encoded_header, encoded_payload, encoded_signature = parts
    try:
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    except UnicodeEncodeError as exc:
        raise AuthenticationError("invalid authentication token") from exc
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    supplied = _b64decode(encoded_signature)
    if not hmac.compare_digest(expected, supplied):
        raise AuthenticationError("invalid authentication token")
    try:
        header = json.loads(_b64decode(encoded_header))
        payload = json.loads(_b64decode(encoded_payload))
    except (AuthenticationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("invalid authentication token") from exc
    if not isinstance(header, dict) or header.get("alg") != TOKEN_ALGORITHM or header.get("typ") != "JWT":
        raise AuthenticationError("invalid authentication token")
    if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str):
        raise AuthenticationError("invalid authentication token")
    if not isinstance(payload.get("exp"), int) or payload["exp"] <= int(time.time()):
        raise AuthenticationError("authentication token expired")
    return payload
