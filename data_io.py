"""药品与操作日志 CSV 导出：只使用标准库和 MedicineDB 的公开业务接口。

未来 Web 调用方应从已验证的身份取得 user_id，并由服务器决定输出目录。
本模块不连接数据库、不编写 SQL，也不修改药品或操作日志。
"""

import csv
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


CSV_FIELDS = ("id", "nfc_uuid", "name", "location", "expires_on", "status", "is_active")
LOG_CSV_FIELDS = ("日志ID", "操作时间（北京时间）", "操作时间（原始UTC）", "操作用户ID", "操作用户名",
                  "操作类型", "药品ID", "药品名称（当前）", "NFC UUID（完整）", "操作数量")
BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _safe_csv_value(value):
    """仅在导出副本中添加文本前缀，避免表格软件把文本当作公式。"""
    if not isinstance(value, str) or not value:
        return value
    # 跳过可能遮住公式开头的空白、控制字符和 BOM，再检查公式标记。
    index = 0
    while index < len(value) and (
        value[index].isspace() or ord(value[index]) < 32 or value[index] == "\ufeff"
    ):
        index += 1
    probe = value[index:]
    if probe.startswith(("=", "+", "-", "@")) or ord(value[0]) < 32:
        return "'" + value
    return value


def _output_path(output_path):
    try:
        raw = os.fspath(output_path)
    except TypeError as error:
        raise ValueError("保存路径必须是非空字符串或 Path 对象") from error
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValueError("保存路径为空或包含非法字符")
    if raw.endswith(os.sep) or (os.altsep and raw.endswith(os.altsep)):
        raise ValueError("保存路径必须包含文件名")
    try:
        target = Path(raw).expanduser()
    except (ValueError, RuntimeError) as error:
        raise ValueError("保存路径无效") from error
    if not target.is_absolute():
        target = Path.cwd() / target
    parent = target.parent
    if not parent.exists():
        raise FileNotFoundError("目标目录不存在，请先选择已有目录：" + str(parent))
    if not parent.is_dir():
        raise NotADirectoryError("目标的父路径不是目录：" + str(parent))
    # 规范化父目录，但不解析目标文件的符号链接，避免遗漏已有链接。
    target = parent.resolve(strict=True) / target.name
    if target.is_symlink():
        raise FileExistsError("目标文件已存在，拒绝覆盖：" + str(target))
    if target.is_dir():
        raise IsADirectoryError("保存路径指向目录，请指定 CSV 文件名：" + str(target))
    if target.exists():
        raise FileExistsError("目标文件已存在，拒绝覆盖：" + str(target))
    return target


def export_medicines_csv(db, user_id, output_path):
    """管理员导出全部药品，返回 {"path": 绝对路径, "record_count": 数量}。

    staff/viewer 或未知用户抛出 PermissionError；参数和文件错误明确抛出。
    数据库查询异常原样传递；查询成功后才开始写文件。
    UTF-8 BOM、固定英文表头；包括在库、已领取、停用和过期记录。
    """
    db.check_permission(user_id, ("admin",))
    target = _output_path(output_path)
    medicines = db.get_medicines(user_id)
    return _write_csv(target, CSV_FIELDS, medicines)


def _write_csv(target, fields, rows):
    """两个导出共用原子文件发布和公式防护；rows 可以是一次性逐行迭代器。"""
    temporary = None
    count = 0
    try:
        # 同目录保证临时文件与目标在同一文件系统，失败时不留下半成品目标。
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="",
            prefix=".medicine-csv-", suffix=".tmp", dir=target.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: _safe_csv_value(row[field]) for field in fields})
                count += 1
            output.flush()
            os.fsync(output.fileno())
        # 硬链接原子发布完整文件；即使检查后其他请求创建目标，也绝不覆盖。
        # 不使用 os.replace，因为它会覆盖已有目标。文件系统不支持时直接报错。
        os.link(temporary, target)
    except FileExistsError as error:
        raise FileExistsError("目标文件已存在，拒绝覆盖：" + str(target)) from error
    except PermissionError as error:
        raise PermissionError("没有权限写入 CSV 文件：" + str(target)) from error
    except OSError as error:
        raise OSError("CSV 文件保存失败：" + str(error)) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": str(target), "record_count": count}


def _beijing_time(raw_utc):
    # SQLite默认时间为UTC。无法解析的旧值原样保留在UTC列，本地时间留空，不编造时间。
    try:
        value = datetime.strptime(raw_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return value.astimezone(BUSINESS_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError):
        return ""


def export_operation_logs_csv(db, user_id, output_path):
    """导出账号权限内全部原始日志；admin全部、staff本人、viewer拒绝。

    权限与排序由业务层同一读事务决定，不接受筛选范围或客户端角色。
    每行即写入文件，Python不保存完整日志列表。药品名称明确标注为当前名称。
    返回服务器内部路径及记录数；HTTP调用方只发送附件，不将路径返回给客户端。
    """
    with db.iter_operation_logs(user_id) as logs:
        target = _output_path(output_path)
        rows = (dict(zip(LOG_CSV_FIELDS, (
            row["id"], _beijing_time(row["created_at"]), row["created_at"], row["user_id"], row["username"],
            row["action"], row["medicine_id"], row["medicine_name"], row["nfc_uuid"], row["quantity"],
        ))) for row in logs)
        return _write_csv(target, LOG_CSV_FIELDS, rows)
