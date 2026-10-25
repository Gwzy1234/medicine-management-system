"""HTTP 输入、输出约定；拒绝额外字段，避免客户端伪造身份或药品状态。"""

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, field_validator

from medicine_db import SQLITE_MAX_ID, USERNAME_MAX_LENGTH, _expiry, _username


Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]
# 先用业务层统一去除首尾空白，避免不同库对控制空白字符的定义不同。
Username = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=USERNAME_MAX_LENGTH), BeforeValidator(_username)]
Role = Literal["admin", "staff", "viewer"]
Action = Literal["register", "dispense", "disable", "enable"]
Category = Literal["all", "in_stock", "dispensed", "expired", "expiring", "disabled"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginInput(InputModel):
    username: Username
    password: Annotated[str, Field(strict=True, min_length=1, max_length=1024, repr=False)]


class UserCreate(InputModel):
    username: Username
    password: Annotated[str, Field(strict=True, min_length=8, max_length=1024, repr=False)]
    role: Role = "staff"


class MedicineDetails(InputModel):
    name: Text
    location: Text
    expires_on: Annotated[str, Field(strict=True)]

    @field_validator("expires_on")
    @classmethod
    def validate_expiry(cls, value):
        # 复用原业务层的日期校验，转换为 HTTP 422，不另定有效期规则。
        return _expiry(value)


class MedicineCreate(MedicineDetails):
    location: Text = "未指定"


class MedicineBatchCreate(MedicineCreate):
    quantity: Annotated[int, Field(strict=True, ge=1, le=100)] = 1


class NFCInput(InputModel):
    nfc_uuid: Text


class DispenseInput(MedicineDetails):
    # 不 trim 确认文本，避免比较的内容与确认框实际显示内容不同。
    name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    location: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    nfc_uuid: Text

    @field_validator("name", "location")
    @classmethod
    def nonblank_confirmation(cls, value):
        if not value.strip():
            raise ValueError("确认资料不能为空")
        return value


class SimulationInput(InputModel):
    name: Text


class MedicineFilters(InputModel):
    name: Text | None = None
    category: Category = "all"
    days: Annotated[int, Field(ge=0, le=3650)] = 30


class NameFilter(InputModel):
    name: Text


class LogFilters(InputModel):
    user_id: Annotated[int, Field(gt=0, le=SQLITE_MAX_ID)] | None = None
    medicine_id: Annotated[int, Field(gt=0, le=SQLITE_MAX_ID)] | None = None
    action: Action | None = None


class UserOut(BaseModel):
    id: int
    username: str
    role: Role


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    expires_in: int
    user: UserOut


class MedicineOut(BaseModel):
    id: int
    nfc_uuid: str
    name: str
    location: str
    expires_on: str
    status: Literal["in_stock", "dispensed"]
    is_active: Literal[0, 1]


class StockOut(BaseModel):
    name: str
    stock: int
    available_stock: int


class MedicineBatchOut(BaseModel):
    created_count: int
    medicines: list[MedicineOut]


class ScanOut(MedicineOut):
    business_date: str
    can_dispense: bool


class DashboardOut(BaseModel):
    business_date: str
    inventory: list[StockOut]
    medicines: list[MedicineOut]
    registered_count: int
    dispensed_count: int


class ToggleOut(BaseModel):
    changed: bool
    medicine: MedicineOut


class DispenseOut(BaseModel):
    success: Literal[True]
    reason: Literal["dispensed"]
    medicine: MedicineOut


class LogOut(BaseModel):
    id: int
    user_id: int
    username: str
    medicine_id: int
    nfc_uuid: str
    medicine_name: str
    action: Action
    quantity: int
    created_at: str


T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    data: T


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: list[dict] | None = None


class ErrorResponse(BaseModel):
    error: ErrorInfo
