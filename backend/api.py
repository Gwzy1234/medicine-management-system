"""HTTP 适配：只调用现有业务方法，不生成 UUID、不计算库存、不另写 SQL。"""

import tempfile
from datetime import datetime
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from starlette.responses import FileResponse

import data_io
from medicine_db import SQLITE_MAX_ID
from .auth import create_access_token, current_user, get_db, require_admin, require_operator
from .errors import APIError
from .schemas import (
    DashboardOut, DispenseInput, DispenseOut, Envelope, LogFilters, LogOut, LoginInput, MedicineCreate, MedicineDetails,
    MedicineBatchCreate, MedicineBatchOut, MedicineFilters, MedicineOut, NameFilter, NFCInput, SimulationInput, StockOut,
    ScanOut, ToggleOut, TokenOut, UserCreate, UserOut,
)


router = APIRouter(prefix="/api")
PositiveID = Annotated[int, Path(gt=0, le=SQLITE_MAX_ID)]


def no_query_parameters(request: Request):
    if request.query_params:
        raise APIError(422, "unexpected_query", "此接口不接受查询参数")


NO_QUERY = [Depends(no_query_parameters)]


def not_found(entity="药品"):
    return APIError(404, "not_found", entity + "不存在")


@router.post("/auth/login", response_model=Envelope[TokenOut], dependencies=NO_QUERY)
def login(body: LoginInput, request: Request, db=Depends(get_db)):
    user = db.login_user(body.username, body.password)
    if user is None:
        raise APIError(401, "invalid_credentials", "用户名或密码错误", {"WWW-Authenticate": "Bearer"})
    token = create_access_token(user["id"], request.app.state.settings)
    return {"data": {**token, "user": user}}


@router.get("/auth/me", response_model=Envelope[UserOut], dependencies=NO_QUERY)
def me(user=Depends(current_user)):
    return {"data": user}


@router.get("/users", response_model=Envelope[list[UserOut]], dependencies=NO_QUERY)
def users(user=Depends(require_admin), db=Depends(get_db)):
    return {"data": db.get_users(user["id"])}


@router.post("/users", status_code=201, response_model=Envelope[UserOut], dependencies=NO_QUERY)
def create_user(body: UserCreate, user=Depends(require_admin), db=Depends(get_db)):
    return {"data": db.create_user(user["id"], body.username, body.password, body.role)}


@router.get("/users/{user_id}", response_model=Envelope[UserOut], dependencies=NO_QUERY)
def get_user(user_id: PositiveID, user=Depends(require_admin), db=Depends(get_db)):
    result = db.get_user_by_id(user_id, requester_id=user["id"])
    if result is None:
        raise not_found("用户")
    return {"data": result}


@router.get("/medicines", response_model=Envelope[list[MedicineOut]])
def medicines(filters: Annotated[MedicineFilters, Query()], user=Depends(current_user), db=Depends(get_db)):
    # 组合条件交由业务层在同一个读事务中处理，接口层不另开查询。
    return {"data": db.get_medicines_filtered(
        user["id"], name=filters.name, category=filters.category, days=filters.days,
    )}


@router.post("/medicines/batch", status_code=201, response_model=Envelope[MedicineBatchOut], dependencies=NO_QUERY)
def register_batch(body: MedicineBatchCreate, user=Depends(require_admin), db=Depends(get_db)):
    # 静态路径放在动态 ID 路由之前；整批只进入一个业务写事务。
    return {"data": db.add_medicines_batch(
        name=body.name, expires_on=body.expires_on, quantity=body.quantity,
        user_id=user["id"], location=body.location,
    )}


@router.get("/medicines/{medicine_id}", response_model=Envelope[MedicineOut], dependencies=NO_QUERY)
def get_medicine(medicine_id: PositiveID, user=Depends(current_user), db=Depends(get_db)):
    result = db.get_medicine_by_id(medicine_id, user["id"])
    if result is None:
        raise not_found()
    return {"data": result}


@router.post("/medicines", status_code=201, response_model=Envelope[MedicineOut], dependencies=NO_QUERY)
def register(body: MedicineCreate, user=Depends(require_admin), db=Depends(get_db)):
    return {"data": db.add_medicine(
        name=body.name, expires_on=body.expires_on, user_id=user["id"], location=body.location,
    )}


@router.patch("/medicines/{medicine_id}", response_model=Envelope[MedicineOut], dependencies=NO_QUERY)
def edit(medicine_id: PositiveID, body: MedicineDetails, user=Depends(require_admin), db=Depends(get_db)):
    # 三个可编辑字段一起提交，直接进入现有原子方法，不在接口层合并旧值。
    try:
        result = db.update_medicine_details(
            medicine_id=medicine_id, name=body.name, location=body.location,
            expires_on=body.expires_on, user_id=user["id"],
        )
    except LookupError as error:
        if type(error) is not LookupError:
            raise
        raise not_found() from error
    except ValueError as error:
        # 当前业务层对此状态抛出 ValueError；其余参数异常仍按 400 处理。
        if str(error) == "已领取药品不允许修改资料":
            raise APIError(409, "already_dispensed", "已领取药品不允许修改资料") from error
        raise
    return {"data": result}


@router.post("/medicines/{medicine_id}/enable", response_model=Envelope[ToggleOut], dependencies=NO_QUERY)
def enable(medicine_id: PositiveID, user=Depends(require_admin), db=Depends(get_db)):
    try:
        return {"data": db.enable_medicine(medicine_id, user["id"])}
    except LookupError as error:
        if type(error) is not LookupError:
            raise
        raise not_found() from error


@router.post("/medicines/{medicine_id}/disable", response_model=Envelope[ToggleOut], dependencies=NO_QUERY)
def disable(medicine_id: PositiveID, user=Depends(require_admin), db=Depends(get_db)):
    try:
        return {"data": db.disable_medicine(medicine_id, user["id"])}
    except LookupError as error:
        if type(error) is not LookupError:
            raise
        raise not_found() from error


@router.get("/inventory", response_model=Envelope[list[StockOut]], dependencies=NO_QUERY)
def inventory(user=Depends(current_user), db=Depends(get_db)):
    return {"data": db.get_stock_by_name(user["id"])}


@router.get("/dashboard", response_model=Envelope[DashboardOut], dependencies=NO_QUERY)
def dashboard(user=Depends(current_user), db=Depends(get_db)):
    return {"data": db.get_dashboard(user["id"])}


@router.get("/inventory/by-name", response_model=Envelope[StockOut])
def inventory_by_name(filters: Annotated[NameFilter, Query()], user=Depends(current_user), db=Depends(get_db)):
    return {"data": db.get_stock_for_name(filters.name, user["id"])}


@router.post("/nfc/scan", response_model=Envelope[ScanOut], dependencies=NO_QUERY)
def scan(body: NFCInput, user=Depends(current_user), db=Depends(get_db)):
    result = db.get_nfc_scan(user["id"], nfc_uuid=body.nfc_uuid)
    if result is None:
        raise not_found()
    return {"data": result}


@router.post("/nfc/simulate", response_model=Envelope[ScanOut], dependencies=NO_QUERY)
def simulate(body: SimulationInput, user=Depends(current_user), db=Depends(get_db)):
    result = db.get_nfc_scan(user["id"], name=body.name)
    if result is None:
        raise APIError(404, "no_available_medicine", "没有该名称的可领取药品")
    # 查询不预留药品；后续请求仍须经过统一领取接口重新核验。
    return {"data": result}


@router.post("/nfc/dispense", response_model=Envelope[DispenseOut], dependencies=NO_QUERY)
def dispense(body: DispenseInput, user=Depends(require_operator), db=Depends(get_db)):
    result = db.withdraw_by_nfc(body.nfc_uuid, user["id"], confirmed_details={
        "name": body.name, "location": body.location, "expires_on": body.expires_on,
    })
    if not result["success"]:
        failures = {
            "details_changed": (409, "药品资料已变化，请重新扫描、核对最新资料后再确认领取"),
            "not_found": (404, "药品不存在"),
            "already_dispensed": (409, "药品已经领取"),
            "disabled": (409, "药品已停用"),
            "expired": (409, "药品已过期"),
        }
        if result["reason"] not in failures:
            raise APIError(500, "internal_error", "领取失败，请联系管理员")
        status, message = failures[result["reason"]]
        raise APIError(status, result["reason"], message)
    return {"data": result}


@router.get("/logs", response_model=Envelope[list[LogOut]])
def logs(filters: Annotated[LogFilters, Query()], user=Depends(require_operator), db=Depends(get_db)):
    return {"data": db.get_operation_logs(
        user_id=user["id"], filter_user_id=filters.user_id,
        medicine_id=filters.medicine_id, action=filters.action,
    )}


class TemporaryCSVResponse(FileResponse):
    """正常完成或发送异常都清理本次临时目录，不保留下载文件。"""

    def __init__(self, path, temporary, *, filename="medicines.csv"):
        super().__init__(path, media_type="text/csv; charset=utf-8", filename=filename,
                         headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        self.temporary = temporary

    async def __call__(self, scope, receive, send):
        # 由本响应直接读文件，避免交给服务器延迟读取路径后提前清理。
        scope = dict(scope, extensions={k: v for k, v in scope.get("extensions", {}).items()
                                       if k != "http.response.pathsend"})
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.temporary.cleanup()


@router.get("/export/medicines", response_class=FileResponse, dependencies=NO_QUERY)
def export(request: Request, user=Depends(require_admin), db=Depends(get_db)):
    # 每次导出都是一次性附件，不提供断点续传；避免 FileResponse 的文本错误破坏 JSON 错误约定。
    if "range" in request.headers:
        raise APIError(400, "unsupported_range", "CSV 导出不支持分段下载，请重新完整下载")
    temporary = None
    try:
        temporary = tempfile.TemporaryDirectory(prefix="medicine-api-csv-")
        result = data_io.export_medicines_csv(db, user["id"], FilePath(temporary.name) / "medicines.csv")
        return TemporaryCSVResponse(result["path"], temporary)
    except BaseException as error:
        if temporary is not None:
            temporary.cleanup()
        if isinstance(error, OSError):
            if isinstance(error, PermissionError):
                # 区分角色被撤销与服务器文件权限故障，均不暴露路径。
                db.check_permission(user["id"], ("admin",))
            raise APIError(500, "export_failed", "CSV 文件生成失败，请联系管理员") from error
        raise


@router.get("/export/logs", response_class=FileResponse, dependencies=NO_QUERY,
            responses={200: {"description": "权限范围内全部原始日志，UTF-8 BOM CSV附件；药品名称为当前名称",
                             "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}}})
def export_logs(request: Request, user=Depends(require_operator), db=Depends(get_db)):
    """下载权限内全部日志。身份只取JWT核验后的当前用户，不接受任何范围参数。"""
    if "range" in request.headers:
        raise APIError(400, "unsupported_range", "CSV 导出不支持分段下载，请重新完整下载")
    temporary = None
    try:
        temporary = tempfile.TemporaryDirectory(prefix="medicine-logs-csv-")
        result = data_io.export_operation_logs_csv(db, user["id"], FilePath(temporary.name) / "logs.csv")
        filename = datetime.now(data_io.BUSINESS_TIMEZONE).strftime("操作日志_%Y-%m-%d_%H-%M-%S.csv")
        return TemporaryCSVResponse(result["path"], temporary, filename=filename)
    except BaseException as error:
        if temporary is not None:
            temporary.cleanup()
        if isinstance(error, OSError):
            if isinstance(error, PermissionError):
                db.check_permission(user["id"], ("admin", "staff"))
            raise APIError(500, "export_failed", "日志CSV文件生成失败，请联系管理员") from error
        raise
