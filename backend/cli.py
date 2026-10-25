"""显式初始化与首次管理员准备；不会自动创建账号或接受命令行明文密码。"""

import argparse
import getpass
import sys

from medicine_db import MedicineDB
from .config import database_path


def main():
    parser = argparse.ArgumentParser(description="急救药品数据库准备工具")
    parser.add_argument("--db-path", help="数据库路径；默认读取 MEDICINE_DB_PATH")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="显式初始化或校验数据库，不创建用户")
    create = subparsers.add_parser("create-admin", help="仅用户表为空时创建首位管理员")
    create.add_argument("--username", required=True)
    arguments = parser.parse_args()
    try:
        db = MedicineDB(database_path(arguments.db_path))
        if arguments.command == "init-db":
            db.init_db()
            print("数据库初始化或校验成功；没有创建默认账号。")
        else:
            if not db.path.is_file():
                raise ValueError("请先运行 init-db")
            password = getpass.getpass("管理员密码（至少8字符，建议使用随机强密码）：")
            repeated = getpass.getpass("再次输入密码：")
            if password != repeated:
                raise ValueError("两次密码输入不一致")
            db.create_first_admin(arguments.username, password)
            print("首位管理员创建成功。")
    except Exception as error:
        # 不输出输入内容或底层文件路径。可预期的业务校验提示才允许显示。
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        print("操作失败：" + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
