"""急救药品管理：只依赖 Python 标准库的数据库业务层。

使用顺序：db = MedicineDB() -> db.init_db() -> 首次创建管理员 -> 业务调用。
导入模块或创建对象不会创建、初始化或修改数据库。
所有业务 SQL 从 queries.sql 加载；Python 只写连接、事务及结构检查语句。

未来 Web 层必须从已验证的会话取得 user_id；传入一个 ID 本身不等于登录。
不依赖或修改本目录已有的 backend 包，不实现 HTTP、会话或 NFC 硬件读写。
"""

import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "medical.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
QUERIES_PATH = BASE_DIR / "queries.sql"
HASH_ITERATIONS = 600_000
SQLITE_MAX_ID = 2**63 - 1
USERNAME_MAX_LENGTH = 500
ROLES = ("admin", "staff", "viewer")
ACTIONS = ("register", "dispense", "disable", "enable")


class DatabaseInitializationError(RuntimeError):
    """已有库结构不匹配，或初始化失败；不会自动迁移或重建。"""


class AlreadyInitializedError(RuntimeError):
    """用户表已非空，不能再次创建首位管理员。"""


def _without_comments(sql):
    # 去除 SQL 注释，但保留字符串中的 --、分号等内容，供加载器检查。
    tokens = r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/"
    return re.sub(
        tokens,
        lambda match: " " if match[0].startswith(("--", "/*")) else match[0],
        sql,
        flags=re.DOTALL,
    )


def load_queries(path=QUERIES_PATH):
    """加载 -- name: 查询，拒绝重名、空语句、坏名称及一个名称下的多条 SQL。"""
    queries = {}
    name = None
    lines = []

    def save_query():
        sql = "\n".join(lines).strip()
        clean = _without_comments(sql).strip()
        statements = 0
        start = 0
        # complete_statement 能识别字符串内部的分号，不用 split(';') 猜测。
        for end, character in enumerate(clean, 1):
            if character == ";" and sqlite3.complete_statement(clean[start:end]):
                statements += 1
                start = end
        if statements != 1 or clean[start:].strip():
            raise ValueError(f"命名 SQL {name} 必须有且仅有一条以分号结束的语句")
        queries[name] = sql

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if re.match(r"^\s*--\s*name:", line):
            if name is not None:
                save_query()
            match = re.fullmatch(r"\s*--\s*name:\s*([a-z][a-z0-9_]*)\s*", line)
            if match is None:
                raise ValueError("命名 SQL 的名称为空或格式错误")
            name = match[1]
            if name in queries:
                raise ValueError(f"命名 SQL 重复：{name}")
            lines = []
        elif name is not None:
            lines.append(line)
        elif _without_comments(line).strip():
            raise ValueError("发现没有 -- name: 名称的 SQL")
    if name is not None:
        save_query()
    if not queries:
        raise ValueError("没有找到命名 SQL")
    return queries


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}必须是非空字符串")
    return value.strip()


def _positive_id(value, label="用户 ID"):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label}必须是正整数")
    if value > SQLITE_MAX_ID:
        raise ValueError(f"{label}不能超过 SQLite 有符号64位整数上限")
    return value


def _username(value):
    """创建与登录统一使用去除首尾空白后的 1～500 字符用户名。

    历史超限账号不自动截断、改名或迁移；如存在，需另行授权更名后登录。
    """
    value = _text(value, "用户名")
    if len(value) > USERNAME_MAX_LENGTH:
        raise ValueError(f"用户名长度必须在 1 到 {USERNAME_MAX_LENGTH} 个字符之间")
    return value


def _medicine_name(value):
    # 登记和名称查询使用相同规则；不擅自合并不同药名、规格和剂型。
    return " ".join(_text(value, "药品名称").split())


def _expiry(value):
    if not isinstance(value, str):
        raise ValueError("有效期必须是 YYYY-MM-DD 字符串")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("有效期必须是实际存在的 YYYY-MM-DD 日期") from error
    if parsed.isoformat() != value:
        raise ValueError("有效期必须使用 YYYY-MM-DD 格式")
    return value


def hash_password(password):
    """保存算法、迭代次数、随机盐和哈希结果；每次调用使用新的盐。"""
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        raise ValueError("密码长度必须在 8 到 1024 个字符之间")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS)
    return f"pbkdf2_sha256${HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    """安全比较密码；明文、损坏或无法识别的存储内容均不能登录。"""
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        return False
    if not isinstance(encoded, str):
        return False
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$")
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_text)
        expected = bytes.fromhex(digest_text)
        # 防止损坏的编码要求无限量计算；当前生成的编码为 600000 次。
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(actual, expected)


def business_today():
    """由服务器生成上海业务日期，不使用客户端传来的日期决定领取资格。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _schema_signature(connection):
    # SQLite 元数据查询只用于结构校验，不属于药品/用户等业务 SQL。
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return [
        (row[0], row[1], row[2], " ".join(_without_comments(row[3] or "").split()))
        for row in rows
    ]


class MedicineDB:
    """每次业务调用使用独立连接；db_path 可指向测试专用临时库。

    today_provider 仅用于测试或服务器配置，不得由 HTTP 请求参数控制。
    数据库异常原样抛出；调用方不得把异常解释成业务成功。
    """

    def __init__(self, db_path=DB_PATH, *, today_provider=business_today, timeout=5.0):
        path = Path(db_path).expanduser()
        self.path = path if path.is_absolute() else BASE_DIR / path
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("数据库等待秒数必须是非负有限数字")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("数据库等待秒数必须是非负有限数字")
        if not callable(today_provider):
            raise ValueError("today_provider 必须可调用")
        self.timeout = timeout
        self.today_provider = today_provider
        self.queries = load_queries()

    def _today(self):
        value = self.today_provider()
        if type(value) is not date:
            raise ValueError("业务日期函数必须返回 datetime.date")
        return value

    def _connect(self, path=None, *, create=False):
        target = self.path if path is None else path
        if target.is_symlink():
            raise DatabaseInitializationError("数据库路径不能是符号链接")
        # 日常连接用 mode=rw，防止路径错误时悄悄创建空数据库。
        mode = "rwc" if create else "rw"
        uri = target.as_uri() + "?mode=" + mode
        connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=self.timeout)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise DatabaseInitializationError("无法启用 SQLite 外键检查")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def connection(self, *, write=False):
        """显式事务：成功提交；任何异常回滚；无论成败都关闭连接。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _execute(self, connection, name, parameters=None):
        return connection.execute(self.queries[name], parameters if parameters is not None else {})

    def init_db(self, *, allow_create=True):
        """显式初始化默认可建库；HTTP 启动传 False，只校验且绝不建库。

        已有匹配库只校验，旧库/空文件/坏库均不迁移、不覆盖。
        """
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        reference = sqlite3.connect(":memory:", isolation_level=None)
        try:
            reference.execute("PRAGMA foreign_keys = ON")
            reference.executescript(schema)
            expected = _schema_signature(reference)
        finally:
            reference.close()

        if self.path.is_symlink():
            raise DatabaseInitializationError("数据库路径不能是符号链接")
        if not self.path.exists():
            if not allow_create:
                raise DatabaseInitializationError("数据库不存在；仅允许校验，未创建数据库")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 先完成临时库，再原子发布；绝不把半成品或空库当作初始化成功。
            with tempfile.TemporaryDirectory(prefix=".medicine-init-", dir=self.path.parent) as tmp:
                temporary = Path(tmp) / "new.sqlite3"
                connection = self._connect(temporary, create=True)
                try:
                    connection.executescript(schema)
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
                try:
                    # 硬链接不会覆盖已有目标，兼顾两个初始化进程同时启动。
                    os.link(temporary, self.path)
                except FileExistsError:
                    pass

        try:
            with self.connection() as connection:
                if _schema_signature(connection) != expected:
                    raise DatabaseInitializationError("已有数据库结构不匹配；未迁移或重建")
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise DatabaseInitializationError("数据库完整性检查失败")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise DatabaseInitializationError("数据库存在无效外键")
        except sqlite3.Error as error:
            raise DatabaseInitializationError("数据库无法打开或校验失败；未重建") from error

    def _check_permission(self, connection, user_id, allowed_roles):
        _positive_id(user_id)
        row = self._execute(connection, "get_user_role", {"user_id": user_id}).fetchone()
        if row is None or row["role"] not in allowed_roles:
            raise PermissionError("用户不存在或没有操作权限")
        return row["role"]

    def check_permission(self, user_id, allowed_roles=ROLES):
        """供可信 Python 调用方检查当前数据库角色；不能替代登录验证。"""
        if not allowed_roles or any(role not in ROLES for role in allowed_roles):
            raise ValueError("权限角色列表无效")
        with self.connection() as connection:
            return self._check_permission(connection, user_id, allowed_roles)

    def create_first_admin(self, username, password):
        username = _username(username)
        encoded = hash_password(password)
        with self.connection(write=True) as connection:
            # 检查和插入都在写锁内，两个并发调用也只能成功一次。
            if self._execute(connection, "get_users").fetchone() is not None:
                raise AlreadyInitializedError("已有用户，禁止重复初始化管理员")
            cursor = self._execute(connection, "create_user", {
                "username": username, "password_hash": encoded, "role": "admin",
            })
            user = {"id": cursor.lastrowid, "username": username, "role": "admin"}
        return user

    def create_user(self, admin_id, username, password, role="staff"):
        username = _username(username)
        if role not in ROLES:
            raise ValueError("用户角色无效")
        encoded = hash_password(password)
        with self.connection(write=True) as connection:
            self._check_permission(connection, admin_id, ("admin",))
            cursor = self._execute(connection, "create_user", {
                "username": username, "password_hash": encoded, "role": role,
            })
            user = {"id": cursor.lastrowid, "username": username, "role": role}
        return user

    def login_user(self, username, password):
        try:
            username = _username(username)
        except ValueError:
            return None
        with self.connection() as connection:
            row = self._execute(connection, "get_user_by_username", {"username": username}).fetchone()
        if row is None or not verify_password(password, row["password"]):
            return None
        return {"id": row["id"], "username": row["username"], "role": row["role"]}

    def get_user_by_id(self, user_id, requester_id):
        """管理员可查询任意用户；其他用户只可查询本人，不返回密码。"""
        _positive_id(user_id)
        with self.connection() as connection:
            role = self._check_permission(connection, requester_id, ROLES)
            if role != "admin" and user_id != requester_id:
                raise PermissionError("只能查询本人信息")
            row = self._execute(connection, "get_user_by_id", {"user_id": user_id}).fetchone()
        return dict(row) if row is not None else None

    def get_users(self, user_id):
        with self.connection() as connection:
            self._check_permission(connection, user_id, ("admin",))
            return [dict(row) for row in self._execute(connection, "get_users")]

    def _log(self, connection, user_id, medicine_id, action):
        # quantity=1 表示涉及一支药品；启停日志不代表库存增减。
        self._execute(connection, "add_operation_log", {
            "user_id": user_id, "medicine_id": medicine_id, "action": action, "quantity": 1,
        })

    def add_medicine(self, name, expires_on, user_id, location="未指定"):
        name = _medicine_name(name)
        location = _text(location, "存放位置")
        expires_on = _expiry(expires_on)
        with self.connection(write=True) as connection:
            self._check_permission(connection, user_id, ("admin",))
            nfc_uuid = str(uuid4())
            cursor = self._execute(connection, "add_medicine", {
                "nfc_uuid": nfc_uuid, "name": name, "location": location, "expires_on": expires_on,
            })
            medicine_id = cursor.lastrowid
            self._log(connection, user_id, medicine_id, "register")
            row = self._execute(connection, "get_medicine_by_id", {"medicine_id": medicine_id}).fetchone()
            medicine = dict(row)
        return medicine

    def add_medicines_batch(self, name, expires_on, quantity, user_id, location="未指定"):
        """管理员整批登记 1～100 支；全部药品和逐支登记日志一起提交或回滚。

        不调用会独立提交的 add_medicine；返回值只在整个事务提交成功后生成。
        """
        name = _medicine_name(name)
        location = _text(location, "存放位置")
        expires_on = _expiry(expires_on)
        if type(quantity) is not int or not 1 <= quantity <= 100:
            raise ValueError("登记数量必须是 1～100 的整数，不能是布尔值")
        medicines = []
        with self.connection(write=True) as connection:
            self._check_permission(connection, user_id, ("admin",))
            for _ in range(quantity):
                cursor = self._execute(connection, "add_medicine", {
                    "nfc_uuid": str(uuid4()), "name": name, "location": location, "expires_on": expires_on,
                })
                medicine_id = cursor.lastrowid
                self._log(connection, user_id, medicine_id, "register")
                row = self._execute(connection, "get_medicine_by_id", {"medicine_id": medicine_id}).fetchone()
                if row is None:
                    raise RuntimeError("无法读取批量登记记录，已取消整批登记")
                medicines.append(dict(row))
        return {"created_count": len(medicines), "medicines": medicines}

    def update_medicine_details(self, medicine_id, name, location, expires_on, user_id):
        """管理员编辑在库药品的三项资料，提交后返回重新查询的药品字典。

        不存在时抛出 LookupError，已领取时抛出 ValueError。
        当前日志 action 不支持资料编辑，因此不写日志，也不借用其他动作。
        """
        _positive_id(medicine_id, "药品 ID")
        name = _medicine_name(name)
        location = _text(location, "存放位置")
        expires_on = _expiry(expires_on)
        with self.connection(write=True) as connection:
            self._check_permission(connection, user_id, ("admin",))
            cursor = self._execute(connection, "update_medicine_details", {
                "medicine_id": medicine_id, "name": name,
                "location": location, "expires_on": expires_on,
            })
            if cursor.rowcount != 1:
                if cursor.rowcount == 0:
                    existing = self._execute(connection, "get_medicine_by_id", {
                        "medicine_id": medicine_id,
                    }).fetchone()
                    if existing is None:
                        raise LookupError("药品不存在")
                    if existing["status"] == "dispensed":
                        raise ValueError("已领取药品不允许修改资料")
                raise RuntimeError("药品资料更新未恰好影响一行，已取消修改")
            row = self._execute(connection, "get_medicine_by_id", {
                "medicine_id": medicine_id,
            }).fetchone()
            if row is None:
                raise RuntimeError("更新后无法读取药品资料，已取消修改")
            medicine = dict(row)
        return medicine

    def _medicine_query(self, name, user_id, parameters=None, *, one=False):
        with self.connection() as connection:
            self._check_permission(connection, user_id, ROLES)
            cursor = self._execute(connection, name, parameters)
            if one:
                row = cursor.fetchone()
                return dict(row) if row is not None else None
            return [dict(row) for row in cursor]

    def get_medicines(self, user_id):
        return self._medicine_query("get_medicines", user_id)

    def get_medicines_filtered(self, user_id, *, name=None, category="all", days=30):
        """复用命名查询，在同一读事务中按名称、状态取交集，保留原排序。"""
        queries = {
            "all": "get_medicines", "in_stock": "get_in_stock_medicines",
            "dispensed": "get_dispensed_medicines", "expired": "get_expired_medicines",
            "expiring": "get_expiring_medicines", "disabled": "get_disabled_medicines",
        }
        if category not in queries:
            raise ValueError("药品筛选状态无效")
        if name is not None:
            name = _medicine_name(name)
        with self.connection() as connection:
            self._check_permission(connection, user_id, ROLES)
            if category == "all" and name is not None:
                return [dict(row) for row in self._execute(connection, "get_medicines_by_name", {"name": name})]
            parameters = {}
            if category == "expired":
                parameters = {"today": self._today().isoformat()}
            elif category == "expiring":
                parameters = self._expiring_parameters(days)
            rows = [dict(row) for row in self._execute(connection, queries[category], parameters)]
            if name is not None:
                selected = {row["id"] for row in self._execute(connection, "get_medicines_by_name", {"name": name})}
                rows = [row for row in rows if row["id"] in selected]
        return rows

    def get_medicine_by_id(self, medicine_id, user_id):
        _positive_id(medicine_id, "药品 ID")
        return self._medicine_query("get_medicine_by_id", user_id, {"medicine_id": medicine_id}, one=True)

    def get_medicine_by_nfc(self, nfc_uuid, user_id):
        return self._medicine_query("get_medicine_by_nfc", user_id, {"nfc_uuid": _text(nfc_uuid, "UUID")}, one=True)

    def get_medicines_by_name(self, name, user_id):
        return self._medicine_query("get_medicines_by_name", user_id, {"name": _medicine_name(name)})

    def get_in_stock_medicines(self, user_id):
        return self._medicine_query("get_in_stock_medicines", user_id)

    def get_dispensed_medicines(self, user_id):
        return self._medicine_query("get_dispensed_medicines", user_id)

    def get_disabled_medicines(self, user_id):
        return self._medicine_query("get_disabled_medicines", user_id)

    def get_expired_medicines(self, user_id):
        return self._medicine_query("get_expired_medicines", user_id, {"today": self._today().isoformat()})

    def _expiring_parameters(self, days):
        """单条件和组合查询共用相同的临期日期校验及计算。"""
        if type(days) is not int or days < 0:
            raise ValueError("期限天数必须是非负整数")
        today = self._today()
        try:
            deadline = today + timedelta(days=days)
        except OverflowError as error:
            raise ValueError("期限超出了日期范围") from error
        return {"today": today.isoformat(), "deadline": deadline.isoformat()}

    def get_expiring_medicines(self, user_id, days=30):
        return self._medicine_query("get_expiring_medicines", user_id, self._expiring_parameters(days))

    def get_stock_by_name(self, user_id):
        return self._medicine_query("get_stock_by_name", user_id, {"today": self._today().isoformat()})

    def get_dashboard(self, user_id):
        """总览所有统计和明细共用一个读事务、一个业务日期。"""
        with self.connection() as connection:
            self._check_permission(connection, user_id, ROLES)
            today = self._today().isoformat()
            inventory = [dict(row) for row in self._execute(connection, "get_stock_by_name", {"today": today})]
            medicines = [dict(row) for row in self._execute(connection, "get_medicines")]
        return {"business_date": today, "inventory": inventory, "medicines": medicines,
                "registered_count": len(medicines),
                "dispensed_count": sum(row["status"] == "dispensed" for row in medicines)}

    def get_nfc_scan(self, user_id, *, nfc_uuid=None, name=None):
        """只读识别：服务器在同一快照中返回资料、业务日期和领取资格。

        资格只表示识别时的状态，不预留药品；最终写事务仍重新核验。
        """
        if (nfc_uuid is None) == (name is None):
            raise ValueError("扫描必须且只能提供 UUID 或药品名称")
        if nfc_uuid is not None:
            nfc_uuid = _text(nfc_uuid, "UUID")
        else:
            name = _medicine_name(name)
        with self.connection() as connection:
            self._check_permission(connection, user_id, ROLES)
            today = self._today().isoformat()
            if nfc_uuid is not None:
                row = self._execute(connection, "get_medicine_by_nfc", {"nfc_uuid": nfc_uuid}).fetchone()
            else:
                rows = self._execute(connection, "get_medicines_by_name", {"name": name})
                row = next((r for r in rows if r["status"] == "in_stock" and r["is_active"] == 1
                            and r["expires_on"] >= today), None)
            if row is None:
                return None
            return {**dict(row), "business_date": today,
                    "can_dispense": row["status"] == "in_stock" and row["is_active"] == 1 and row["expires_on"] >= today}

    def get_stock_for_name(self, name, user_id):
        """两种库存必须在同一读事务中统计，避免读到不同提交版本。"""
        name = _medicine_name(name)
        with self.connection() as connection:
            self._check_permission(connection, user_id, ROLES)
            today = self._today().isoformat()
            stock = self._execute(connection, "get_book_stock_by_name", {"name": name}).fetchone()["stock"]
            available = self._execute(connection, "get_available_stock_by_name", {
                "name": name, "today": today,
            }).fetchone()["available_stock"]
        return {"name": name, "stock": stock, "available_stock": available}

    def find_simulated_nfc_uuid(self, name, user_id):
        """模拟入口只挑选已登记、可领取的 UUID；选中后仍须调用 withdraw_by_nfc。

        查询结果不会预留药品。若期间已被领取，统一领取函数会正常拒绝。
        """
        name = _medicine_name(name)
        with self.connection() as connection:
            self._check_permission(connection, user_id, ROLES)
            today = self._today().isoformat()
            rows = self._execute(connection, "get_medicines_by_name", {"name": name})
            for row in rows:
                if row["status"] == "in_stock" and row["is_active"] == 1 and row["expires_on"] >= today:
                    return row["nfc_uuid"]
        return None

    def withdraw_by_nfc(self, nfc_uuid, user_id, *, confirmed_details=None):
        """真实/模拟扫描唯一领取入口。

        成功：success=True、reason=dispensed、medicine 为领取后的药品信息。
        业务拒绝：success=False，reason 包括 details_changed 及原有状态原因。
        权限、参数及数据库异常直接抛出；不会返回虚假的成功结果。
        HTTP 必须提供确认的三项原始资料。None 仅保留受信任的旧 Python 调用兼容，
        HTTP 路由没有该降级入口，不能将客户端输入转为 None 绕过确认保护。
        """
        nfc_uuid = _text(nfc_uuid, "UUID")
        if confirmed_details is not None:
            fields = ("name", "location", "expires_on")
            if not isinstance(confirmed_details, dict) or set(confirmed_details) != set(fields):
                raise ValueError("领取确认必须包含名称、位置及有效期")
            # 校验但不规范化确认副本：必须与用户实际看见的资料逐字比较。
            _text(confirmed_details["name"], "确认名称")
            _text(confirmed_details["location"], "确认位置")
            _expiry(confirmed_details["expires_on"])
            confirmed_details = dict(confirmed_details)
        with self.connection(write=True) as connection:
            self._check_permission(connection, user_id, ("admin", "staff"))
            if confirmed_details is not None:
                current = self._execute(connection, "get_medicine_by_nfc", {"nfc_uuid": nfc_uuid}).fetchone()
                if current is not None and any(current[key] != confirmed_details[key] for key in fields):
                    return {"success": False, "reason": "details_changed", "medicine": None}
            # 获取写锁后才生成日期，避免锁等待跨过午夜时沿用过期日期。
            today = self._today().isoformat()
            params = {"nfc_uuid": nfc_uuid, "today": today}
            row = self._execute(connection, "get_dispensable_medicine_by_nfc", params).fetchone()
            if row is None:
                existing = self._execute(connection, "get_medicine_by_nfc", {"nfc_uuid": nfc_uuid}).fetchone()
                if existing is None:
                    reason = "not_found"
                elif existing["status"] == "dispensed":
                    reason = "already_dispensed"
                elif existing["is_active"] == 0:
                    reason = "disabled"
                else:
                    reason = "expired"
                return {"success": False, "reason": reason, "medicine": None}

            cursor = self._execute(connection, "dispense_medicine_by_nfc", params)
            if cursor.rowcount != 1:
                raise RuntimeError("领取更新未恰好影响一行，已取消领取")
            self._log(connection, user_id, row["id"], "dispense")
            medicine = dict(row)
            medicine["status"] = "dispensed"
        # 只有上下文管理器完成提交，代码才会到这里。
        return {"success": True, "reason": "dispensed", "medicine": medicine}

    def _set_active(self, medicine_id, user_id, *, active):
        _positive_id(medicine_id, "药品 ID")
        action = "enable" if active else "disable"
        query = "enable_medicine" if active else "disable_medicine"
        with self.connection(write=True) as connection:
            self._check_permission(connection, user_id, ("admin",))
            cursor = self._execute(connection, query, {"medicine_id": medicine_id})
            row = self._execute(connection, "get_medicine_by_id", {"medicine_id": medicine_id}).fetchone()
            if row is None:
                raise LookupError("药品不存在")
            changed = cursor.rowcount == 1
            if changed:
                self._log(connection, user_id, medicine_id, action)
            medicine = dict(row)
        return {"changed": changed, "medicine": medicine}

    def enable_medicine(self, medicine_id, user_id):
        return self._set_active(medicine_id, user_id, active=True)

    def disable_medicine(self, medicine_id, user_id):
        return self._set_active(medicine_id, user_id, active=False)

    def get_operation_logs(self, user_id, *, filter_user_id=None, medicine_id=None, action=None):
        """管理员可查全部；staff 只查本人；viewer 禁止。支持组合筛选。"""
        with self.iter_operation_logs(user_id, filter_user_id=filter_user_id, medicine_id=medicine_id, action=action) as rows:
            return list(rows)

    @contextmanager
    def iter_operation_logs(self, user_id, *, filter_user_id=None, medicine_id=None, action=None):
        """在同一只读事务中核验权限并逐行读取；调用方必须用 with，退出即关闭连接。

        列表查询与CSV导出共用本入口，不复制权限规则，也不把全部日志装入内存。
        复用现有命名SQL的时间倒序、ID倒序；药名来自当前关联药品记录。
        """
        if filter_user_id is not None:
            _positive_id(filter_user_id)
        if medicine_id is not None:
            _positive_id(medicine_id, "药品 ID")
        if action is not None and action not in ACTIONS:
            raise ValueError("日志操作类型无效")
        with self.connection() as connection:
            role = self._check_permission(connection, user_id, ("admin", "staff"))
            if role == "staff":
                if filter_user_id is not None and filter_user_id != user_id:
                    raise PermissionError("只能查询本人的操作日志")
                filter_user_id = user_id
            if filter_user_id is not None:
                query, params = "get_operation_logs_by_user", {"user_id": filter_user_id}
            elif medicine_id is not None:
                query, params = "get_operation_logs_by_medicine", {"medicine_id": medicine_id}
            elif action is not None:
                query, params = "get_operation_logs_by_action", {"action": action}
            else:
                query, params = "get_operation_logs", {}
            rows = self._execute(connection, query, params)
            # 现有 SQL 是单维度查询；其余条件在结果中取交集，不拼接新 SQL。
            yield (dict(row) for row in rows
                   if (medicine_id is None or row["medicine_id"] == medicine_id)
                   and (action is None or row["action"] == action))
