"""R1～R4 回归测试：全部账号、数据库和 HTTP 服务均为临时测试资源。"""

import contextlib
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient

import medicine_db as m
from backend import cli
from backend.auth import AUDIENCE, ISSUER, create_access_token
from backend.config import Settings
from backend.main import create_app


PASSWORD = "Fictional-regression-password-2026"
TODAY = date(2026, 10, 22)
MAX_ID = 2**63 - 1


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="medicine-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = m.MedicineDB(self.root / "test.sqlite3", today_provider=lambda: TODAY)
        self.db.init_db()
        self.admin = self.db.create_first_admin("admin", PASSWORD)["id"]
        self.settings = Settings(self.db.path, secrets.token_urlsafe(48))
        self.client = self.enterContext(TestClient(create_app(self.settings, self.db), raise_server_exceptions=False))
        self.headers = {"Authorization": "Bearer " + create_access_token(self.admin, self.settings)["access_token"]}

    def request(self, method, path, **kwargs):
        return self.client.request(method, path, headers=self.headers, **kwargs)

    def id_requests(self, value):
        return [
            ("GET", f"/api/users/{value}", {}),
            ("GET", f"/api/medicines/{value}", {}),
            ("PATCH", f"/api/medicines/{value}", {"json": {"name": "虚构药品", "location": "柜", "expires_on": "2030-01-01"}}),
            ("POST", f"/api/medicines/{value}/enable", {}),
            ("POST", f"/api/medicines/{value}/disable", {}),
            ("GET", "/api/logs", {"params": {"user_id": value}}),
            ("GET", "/api/logs", {"params": {"medicine_id": value}}),
        ]

    def test_r1_http_oversized_ids_are_422_without_writes(self):
        before = self.db.path.read_bytes()
        for value in (MAX_ID + 1, 10**100):
            for method, path, kwargs in self.id_requests(value):
                with self.subTest(value=value, path=path, kwargs=kwargs):
                    response = self.request(method, path, **kwargs)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json()["error"]["code"], "validation_error")
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_r1_maximum_sqlite_id_remains_valid(self):
        for method, path, kwargs in self.id_requests(MAX_ID):
            with self.subTest(path=path, kwargs=kwargs):
                response = self.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 200 if path == "/api/logs" else 404, response.text)
                if path == "/api/logs":
                    self.assertEqual(response.json()["data"], [])

    def test_r1_business_layer_rejects_oversized_ids(self):
        big = MAX_ID + 1
        calls = [
            lambda: self.db.check_permission(big),
            lambda: self.db.get_users(big),
            lambda: self.db.get_user_by_id(big, self.admin),
            lambda: self.db.get_user_by_id(self.admin, big),
            lambda: self.db.get_medicines(big),
            lambda: self.db.get_medicine_by_id(big, self.admin),
            lambda: self.db.create_user(big, "forbidden", PASSWORD),
            lambda: self.db.add_medicine("虚构药品", "2030-01-01", big),
            lambda: self.db.update_medicine_details(big, "药品", "柜", "2030-01-01", self.admin),
            lambda: self.db.enable_medicine(big, self.admin),
            lambda: self.db.disable_medicine(big, self.admin),
            lambda: self.db.withdraw_by_nfc("unknown", big),
            lambda: self.db.get_operation_logs(self.admin, filter_user_id=big),
            lambda: self.db.get_operation_logs(self.admin, medicine_id=big),
        ]
        before = self.db.path.read_bytes()
        for index, call in enumerate(calls):
            with self.subTest(index=index), self.assertRaises(ValueError):
                call()
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_r1_oversized_jwt_subject_still_returns_401(self):
        now = int(time.time())
        token = jwt.encode({"sub": str(MAX_ID + 1), "iat": now, "exp": now + 60,
                            "iss": ISSUER, "aud": AUDIENCE}, self.settings.jwt_secret, algorithm="HS256")
        response = self.client.get("/api/auth/me", headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 401)

    def test_r2_concurrent_rename_uses_one_read_snapshot(self):
        row = self.db.add_medicine("旧名称", "2030-01-01", self.admin)
        # WAL 只用于临时夹具，让另一个请求在读事务尚未结束时真实提交。
        connection = self.db._connect()
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        finally:
            connection.close()
        first_read = threading.Event()
        edited = threading.Event()
        original = self.db._execute
        connections = []

        def execute(connection, name, parameters=None):
            cursor = original(connection, name, parameters)
            if name == "get_in_stock_medicines":
                rows = cursor.fetchall()
                connections.append(connection)
                first_read.set()
                if not edited.wait(10):
                    raise RuntimeError("并发编辑未完成")
                return rows
            if name == "get_medicines_by_name":
                connections.append(connection)
            return cursor

        with patch.object(self.db, "_execute", side_effect=execute):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.request, "GET", "/api/medicines",
                                      params={"name": "新名称", "category": "in_stock"})
                self.assertTrue(first_read.wait(10))
                try:
                    response = self.request("PATCH", f'/api/medicines/{row["id"]}',
                                            json={"name": "新名称", "location": "新柜", "expires_on": "2030-01-01"})
                    self.assertEqual(response.status_code, 200, response.text)
                finally:
                    edited.set()
                result = pending.result(timeout=10)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["data"], [])  # 快照中还没有“新名称”。
        self.assertEqual(len(connections), 2)
        self.assertIs(connections[0], connections[1])
        self.assertEqual(self.db.get_medicine_by_id(row["id"], self.admin)["name"], "新名称")
        self.assertEqual(self.request("GET", "/api/medicines", params={"name": "新名称", "category": "in_stock"}).json()["data"][0]["name"], "新名称")

    def test_r2_existing_filters_and_ordering_are_preserved(self):
        self.db.add_medicine("同名药品", "2020-01-01", self.admin)
        self.db.add_medicine("同名药品", "2026-11-01", self.admin)
        disabled = self.db.add_medicine("同名药品", "2030-01-01", self.admin)
        self.db.disable_medicine(disabled["id"], self.admin)
        used = self.db.add_medicine("同名药品", "2031-01-01", self.admin)
        self.db.withdraw_by_nfc(used["nfc_uuid"], self.admin)
        self.db.add_medicine("其他药品", "2030-01-01", self.admin)
        queries = {"all": self.db.get_medicines, "in_stock": self.db.get_in_stock_medicines,
                   "dispensed": self.db.get_dispensed_medicines, "expired": self.db.get_expired_medicines,
                   "expiring": self.db.get_expiring_medicines, "disabled": self.db.get_disabled_medicines}
        before = self.db.path.read_bytes()
        for category, query in queries.items():
            for name in (None, " 同名药品 "):
                with self.subTest(category=category, name=name):
                    expected = query(self.admin)
                    if name is not None:
                        expected = (self.db.get_medicines_by_name(name, self.admin) if category == "all"
                                    else [row for row in expected if row["name"] == name.strip()])
                    parameters = {"category": category}
                    if name is not None:
                        parameters["name"] = name
                    response = self.request("GET", "/api/medicines", params=parameters)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["data"], expected)
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_r2_combined_query_is_one_read_transaction(self):
        trace = []
        original = self.db._connect

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(trace.append)
            return connection

        with patch.object(self.db, "_connect", side_effect=connect):
            response = self.request("GET", "/api/medicines", params={"name": "虚构药品", "category": "expiring", "days": 0})
        self.assertEqual(response.status_code, 200)
        # 当前用户认证有一个读事务；药品组合查询只再使用一个。
        self.assertEqual(trace.count("BEGIN"), 2)
        self.assertEqual(trace.count("COMMIT"), 2)
        self.assertNotIn("BEGIN IMMEDIATE", trace)

    def test_r3_database_moved_after_startup_file_check_is_not_recreated(self):
        db = m.MedicineDB(self.root / "race.sqlite3")
        db.init_db()
        db.create_first_admin("race_admin", PASSWORD)
        before = db.path.read_bytes()
        saved = self.root / "saved.sqlite3"
        original = Path.is_file
        moved = []

        def is_file(path):
            result = original(path)
            if path == db.path and result and not moved:
                path.rename(saved)
                moved.append(True)
            return result

        with patch.object(Path, "is_file", is_file):
            with self.assertRaises(m.DatabaseInitializationError):
                with TestClient(create_app(Settings(db.path, self.settings.jwt_secret), db)):
                    pass
        self.assertEqual(moved, [True])
        self.assertFalse(db.path.exists())
        self.assertEqual(saved.read_bytes(), before)

    def test_r3_no_create_mode_does_not_create_parent_directories(self):
        db = m.MedicineDB(self.root / "missing" / "test.sqlite3")
        with self.assertRaises(m.DatabaseInitializationError):
            db.init_db(allow_create=False)
        self.assertFalse(db.path.parent.exists())

    def test_r3_no_create_mode_preserves_existing_database(self):
        before = self.db.path.read_bytes()
        self.db.init_db(allow_create=False)
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_r3_database_moved_after_exists_check_is_not_recreated(self):
        db = m.MedicineDB(self.root / "late-race.sqlite3")
        db.init_db()
        saved = self.root / "late-saved.sqlite3"
        before = db.path.read_bytes()
        original = Path.exists
        moved = []

        def exists(path):
            result = original(path)
            if path == db.path and result and not moved:
                path.rename(saved)
                moved.append(True)
            return result

        with patch.object(Path, "exists", exists):
            with self.assertRaises(m.DatabaseInitializationError):
                with TestClient(create_app(Settings(db.path, self.settings.jwt_secret), db)):
                    pass
        self.assertEqual(moved, [True])
        self.assertFalse(db.path.exists())
        self.assertEqual(saved.read_bytes(), before)

    def test_r4_business_creation_rejects_overlong_names_before_hashing(self):
        empty = m.MedicineDB(self.root / "empty.sqlite3")
        empty.init_db()
        before = self.db.path.read_bytes()
        with patch.object(m, "hash_password", side_effect=AssertionError("应先校验用户名")):
            for create in (lambda: self.db.create_user(self.admin, "x" * 501, PASSWORD),
                           lambda: empty.create_first_admin("x" * 501, PASSWORD)):
                with self.subTest(create=create), self.assertRaises(ValueError):
                    create()
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_r4_cli_rejects_overlong_name_without_creating_admin(self):
        db = m.MedicineDB(self.root / "cli.sqlite3")
        db.init_db()
        before = db.path.read_bytes()
        arguments = ["cli", "--db-path", str(db.path), "create-admin", "--username", "x" * 501]
        errors = io.StringIO()
        with patch.object(sys, "argv", arguments), patch.object(cli.getpass, "getpass", side_effect=[PASSWORD, PASSWORD]):
            with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
                result = cli.main()
        self.assertEqual(result, 1)
        self.assertIn("500", errors.getvalue())
        self.assertEqual(db.path.read_bytes(), before)

    def test_r4_cli_boundary_username_can_log_in_over_http(self):
        db = m.MedicineDB(self.root / "cli-boundary.sqlite3")
        db.init_db()
        username = "医" * 500
        arguments = ["cli", "--db-path", str(db.path), "create-admin", "--username", " " + username + " "]
        with patch.object(sys, "argv", arguments), patch.object(cli.getpass, "getpass", side_effect=[PASSWORD, PASSWORD]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(), 0)
        with TestClient(create_app(Settings(db.path, self.settings.jwt_secret), db)) as client:
            response = client.post("/api/auth/login", json={"username": " " + username + " ", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["user"]["username"], username)

    def test_r4_http_creation_and_login_share_username_boundaries(self):
        username = "用" * 500
        response = self.request("POST", "/api/users", json={"username": " " + username + " ", "password": PASSWORD})
        self.assertEqual(response.status_code, 201, response.text)
        login = self.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(self.db.login_user(username, PASSWORD)["id"], response.json()["data"]["id"])
        before = self.db.path.read_bytes()
        for username in ("x" * 501, "", "   "):
            self.assertEqual(self.request("POST", "/api/users", json={"username": username, "password": PASSWORD}).status_code, 422)
            self.assertEqual(self.client.post("/api/auth/login", json={"username": username, "password": PASSWORD}).status_code, 422)
            self.assertIsNone(self.db.login_user(username, PASSWORD))
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_r4_legacy_overlong_account_is_never_truncated_or_renamed(self):
        username = "legacy-" + "x" * 501
        # 绕过业务创建仅构造历史夹具，不修改正式库；旧记录不应被静默改名。
        with self.db.connection(write=True) as connection:
            password_hash = self.db._execute(connection, "get_user_by_username", {"username": "admin"}).fetchone()["password"]
            self.db._execute(connection, "create_user", {"username": username, "password_hash": password_hash, "role": "viewer"})
        before = self.db.path.read_bytes()
        self.assertIsNone(self.db.login_user(username, PASSWORD))
        self.assertEqual(self.client.post("/api/auth/login", json={"username": username, "password": PASSWORD}).status_code, 422)
        self.assertEqual(self.db.get_users(self.admin)[-1]["username"], username)
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_r4_control_whitespace_has_identical_cli_business_http_rules(self):
        username = "空" * 500
        created = self.db.create_user(self.admin, "\x1c" + username + "\x1f", PASSWORD)
        self.assertEqual(created["username"], username)
        for whitespace in ("\x1c", "\x1d", "\x1e", "\x1f"):
            with self.subTest(whitespace=ascii(whitespace)):
                value = whitespace + username + whitespace
                self.assertEqual(self.db.login_user(value, PASSWORD)["id"], created["id"])
                response = self.client.post("/api/auth/login", json={"username": value, "password": PASSWORD})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["data"]["user"]["username"], username)
        response = self.request("POST", "/api/users", json={"username": "\x1c" + "名" * 500, "password": PASSWORD})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["data"]["username"], "名" * 500)


class LiveRegressionTests(unittest.TestCase):
    def test_real_http_id_and_username_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="medicine-live-regression-") as tmp:
            root = Path(tmp)
            db = m.MedicineDB(root / "test.sqlite3")
            db.init_db()
            db.create_first_admin("admin", PASSWORD)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            base = f"http://127.0.0.1:{listener.getsockname()[1]}"
            env = dict(os.environ, MEDICINE_DB_PATH=str(db.path), MEDICINE_JWT_SECRET=secrets.token_urlsafe(48),
                       PYTHONDONTWRITEBYTECODE="1")
            with (root / "server.log").open("w") as log:
                process = subprocess.Popen([sys.executable, "-B", "-m", "uvicorn", "backend.main:create_app",
                                            "--factory", "--fd", str(listener.fileno()), "--no-access-log", "--log-level", "warning"],
                                           cwd=m.BASE_DIR, env=env, pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT)
            listener.close()

            def request(method, path, body=None, authorization=None):
                headers = {} if authorization is None else {"Authorization": authorization}
                data = None if body is None else json.dumps(body).encode("utf-8")
                if data is not None:
                    headers["Content-Type"] = "application/json"
                try:
                    response = urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers, method=method), timeout=5)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return response.status, response.read()

            try:
                deadline = time.monotonic() + 15
                while True:
                    self.assertIsNone(process.poll())
                    try:
                        if request("GET", "/docs")[0] == 200:
                            break
                    except OSError:
                        pass
                    if time.monotonic() > deadline:
                        self.fail("临时 Uvicorn 启动超时")
                    time.sleep(0.05)
                status, body = request("POST", "/api/auth/login", {"username": "admin", "password": PASSWORD})
                self.assertEqual(status, 200)
                authorization = "Bearer " + json.loads(body)["data"]["access_token"]
                before = db.path.read_bytes()
                for path in (f"/api/users/{MAX_ID + 1}", f"/api/medicines/{MAX_ID + 1}", f"/api/logs?medicine_id={MAX_ID + 1}"):
                    self.assertEqual(request("GET", path, authorization=authorization)[0], 422)
                self.assertEqual(db.path.read_bytes(), before)
                username = "医" * 500
                self.assertEqual(request("POST", "/api/users", {"username": username, "password": PASSWORD}, authorization)[0], 201)
                self.assertEqual(request("POST", "/api/auth/login", {"username": username, "password": PASSWORD})[0], 200)
                self.assertEqual(request("POST", "/api/users", {"username": "x" * 501, "password": PASSWORD}, authorization)[0], 422)
                self.assertEqual(request("POST", "/api/auth/login", {"username": "x" * 501, "password": PASSWORD})[0], 422)
                self.assertEqual(request("GET", "/api/auth/me", authorization=authorization)[0], 200)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
