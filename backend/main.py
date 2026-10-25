"""应用工厂、启动校验和统一错误响应；导入本模块不会创建应用或数据库。"""

import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from medicine_db import DatabaseInitializationError, MedicineDB
from .api import router
from .config import load_settings
from .errors import APIError
from .schemas import ErrorResponse


logger = logging.getLogger(__name__)


def error_response(status, code, message, *, headers=None, details=None):
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status, headers=headers)


def create_app(settings=None, db=None):
    settings = load_settings() if settings is None else settings
    db = MedicineDB(settings.db_path, timeout=settings.sqlite_timeout) if db is None else db
    if db.path != settings.db_path:
        raise ValueError("业务对象与服务器配置的数据库路径不一致")

    @asynccontextmanager
    async def lifespan(app):
        # 原 init_db 可初始化新库，HTTP 服务只允许验证已经显式准备好的库。
        if db.path.is_symlink() or not db.path.is_file():
            raise DatabaseInitializationError("数据库不存在或不可用，请先显式运行初始化命令")
        db.init_db(allow_create=False)
        yield

    app = FastAPI(
        title="基于Python与NFC的医学影像科急救药品管理系统", version="0.5.0",
        description="扫描只读；两种扫描统一进入 UUID 领取。资料编辑暂不产生操作日志。",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.include_router(router, responses={status: {"model": ErrorResponse}
                                         for status in (400, 401, 403, 404, 409, 422, 500, 503)})

    # 只公开 frontend 资源目录；不能把项目根目录、数据库或 Python 源码挂载出去。
    frontend = Path(__file__).resolve().parents[1] / "frontend"
    app.mount("/static", StaticFiles(directory=frontend), name="frontend")

    frontend_headers = {
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
    }

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(frontend / "index.html", headers=frontend_headers)

    @app.middleware("http")
    async def frontend_html_headers(request, call_next):
        response = await call_next(request)
        # 静态 HTML 别名与首页执行相同策略；不限制 Swagger/Redoc 的独立页面。
        if (request.url.path == "/" or request.url.path.startswith("/static/")) and \
                response.headers.get("content-type", "").startswith("text/html"):
            response.headers.update(frontend_headers)
        return response

    @app.exception_handler(APIError)
    async def api_error(request: Request, error: APIError):
        return error_response(error.status, error.code, error.message, headers=error.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # 不返回 input、ctx 或异常原文，避免把密码、令牌等输入回显。
        details = [{"loc": list(item["loc"]), "type": item["type"]} for item in error.errors()]
        return error_response(422, "validation_error", "请求字段、格式或参数不合法", details=details)

    @app.exception_handler(PermissionError)
    async def permission_error(request: Request, error: PermissionError):
        return error_response(403, "forbidden", "当前用户没有执行此操作的权限")

    @app.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError):
        return error_response(400, "invalid_argument", "业务参数不合法")

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request: Request, error: sqlite3.IntegrityError):
        code = getattr(error, "sqlite_errorcode", 0)
        if code in (sqlite3.SQLITE_CONSTRAINT_UNIQUE, sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY):
            return error_response(409, "data_conflict", "数据与现有记录冲突")
        logger.error("数据库约束请求失败，异常类型：%s", type(error).__name__)
        return error_response(500, "database_error", "数据库操作失败，本次操作未确认成功")

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, error: sqlite3.Error):
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return error_response(503, "database_busy", "数据库繁忙，请稍后重试", headers={"Retry-After": "1"})
        # 不记录请求体、Authorization 或底层异常文本，避免凭据进入日志。
        logger.error("数据库请求失败，异常类型：%s", type(error).__name__)
        return error_response(500, "database_error", "数据库操作失败，本次操作未确认成功")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        return error_response(error.status_code, "http_error", "请求地址不存在或方法不受支持", headers=error.headers)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        logger.error("请求处理失败，异常类型：%s", type(error).__name__)
        return error_response(500, "internal_error", "服务器内部错误，请联系管理员")

    @app.middleware("http")
    async def guard_unexpected_errors(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as error:
            # 在响应开始前处理意外异常，避免上层服务器再次打印可能含输入的异常原文。
            return await unexpected_error(request, error)

    return app
