"""读取服务器配置，不读取客户端配置，也不自动加载 .env。"""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from medicine_db import BASE_DIR, DB_PATH


def database_path(value=None):
    raw = value if value is not None else os.environ.get("MEDICINE_DB_PATH", str(DB_PATH))
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
        raise ValueError("MEDICINE_DB_PATH 必须是非空路径")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    jwt_secret: str = field(repr=False)
    token_minutes: int = 30
    sqlite_timeout: float = 5.0

    def __post_init__(self):
        object.__setattr__(self, "db_path", database_path(self.db_path))
        if not isinstance(self.jwt_secret, str) or not self.jwt_secret.strip():
            raise ValueError("必须配置 MEDICINE_JWT_SECRET")
        if len(self.jwt_secret.encode("utf-8")) < 32:
            raise ValueError("MEDICINE_JWT_SECRET 至少需要 32 字节，建议使用随机密钥")
        if type(self.token_minutes) is not int or not 1 <= self.token_minutes <= 1440:
            raise ValueError("TOKEN_EXPIRE_MINUTES 必须是 1～1440 的整数")
        if isinstance(self.sqlite_timeout, bool) or not isinstance(self.sqlite_timeout, (int, float)):
            raise ValueError("SQLITE_TIMEOUT_SECONDS 必须是非负有限数值")
        if not math.isfinite(self.sqlite_timeout) or not 0 <= self.sqlite_timeout <= 300:
            raise ValueError("SQLITE_TIMEOUT_SECONDS 必须在 0～300 秒之间")


def load_settings():
    try:
        minutes = int(os.environ.get("TOKEN_EXPIRE_MINUTES", "30"))
        timeout = float(os.environ.get("SQLITE_TIMEOUT_SECONDS", "5"))
    except ValueError as error:
        raise ValueError("JWT 有效期或 SQLite 等待时间配置格式错误") from error
    return Settings(database_path(), os.environ.get("MEDICINE_JWT_SECRET", ""), minutes, timeout)
