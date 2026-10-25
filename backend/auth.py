"""JWT 只保存身份；密码验证和当前角色都由原业务层提供。"""

import re
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import APIError


ISSUER = "medicine-api"
AUDIENCE = "medicine-web"
bearer = HTTPBearer(auto_error=False)


def unauthorized():
    return APIError(401, "unauthorized", "请登录，或重新获取有效访问令牌", {"WWW-Authenticate": "Bearer"})


def create_access_token(user_id, settings):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=settings.token_minutes)
    token = jwt.encode({
        "sub": str(user_id), "iat": int(now.timestamp()), "exp": int(expires.timestamp()),
        "iss": ISSUER, "aud": AUDIENCE,
    }, settings.jwt_secret, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "expires_in": settings.token_minutes * 60}


def decode_access_token(token, settings):
    if len(token) > 8192:
        raise unauthorized()
    try:
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"], issuer=ISSUER, audience=AUDIENCE,
            options={"require": ["sub", "iat", "exp", "iss", "aud"]},
        )
        subject = claims["sub"]
        if not isinstance(subject, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", subject):
            raise unauthorized()
        if int(subject) > 9223372036854775807:
            raise unauthorized()
        if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
            raise unauthorized()
        if claims["exp"] <= claims["iat"]:
            raise unauthorized()
        return int(subject)
    except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError) as error:
        raise unauthorized() from error


def get_db(request: Request):
    return request.app.state.db


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db=Depends(get_db),
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized()
    user_id = decode_access_token(credentials.credentials, request.app.state.settings)
    try:
        # 只查询令牌所属用户，原方法会读取数据库中的当前角色，不信任 JWT 内的 role。
        user = db.get_user_by_id(user_id, requester_id=user_id)
    except PermissionError as error:
        raise unauthorized() from error
    if user is None:
        raise unauthorized()
    return user


def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise APIError(403, "forbidden", "此操作仅允许管理员执行")
    return user


def require_operator(user=Depends(current_user)):
    if user["role"] not in ("admin", "staff"):
        raise APIError(403, "forbidden", "当前用户没有领取或日志访问权限")
    return user
