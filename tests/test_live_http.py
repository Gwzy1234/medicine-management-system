"""启动真实 Uvicorn 子进程，经本机 HTTP 验证；所有数据和密钥都属于临时测试。"""

import codecs
import contextlib
import csv
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from medicine_db import BASE_DIR, MedicineDB, business_today
from backend import cli


PASSWORD = "Fictional-live-test-password-2026"


class LiveHTTPTests(unittest.TestCase):
    def test_real_uvicorn_login_register_scan_dispense_logs_and_csv(self):
        with tempfile.TemporaryDirectory(prefix="medicine-live-http-") as tmp:
            root = Path(tmp)
            db = MedicineDB(root / "test.sqlite3")
            db.init_db()
            db.create_first_admin("admin", PASSWORD)
            env = os.environ.copy()
            env.update({"MEDICINE_DB_PATH": str(db.path), "MEDICINE_JWT_SECRET": secrets.token_urlsafe(48),
                        "TOKEN_EXPIRE_MINUTES": "5", "SQLITE_TIMEOUT_SECONDS": "5",
                        "PYTHONDONTWRITEBYTECODE": "1"})
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            log_path = root / "uvicorn.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [sys.executable, "-B", "-m", "uvicorn", "backend.main:create_app", "--factory",
                     "--fd", str(listener.fileno()), "--no-access-log", "--log-level", "warning"],
                    cwd=BASE_DIR, env=env, pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT,
                )
            listener.close()

            def request(method, path, body=None, authorization=None):
                headers = {}
                if authorization is not None:
                    headers["Authorization"] = authorization
                content = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
                if content is not None:
                    headers["Content-Type"] = "application/json"
                message = urllib.request.Request(base + path, data=content, headers=headers, method=method)
                try:
                    response = urllib.request.urlopen(message, timeout=5)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    payload = response.read()
                    result = json.loads(payload) if "application/json" in response.headers.get("Content-Type", "") else payload
                    return response.status, result, dict(response.headers)

            try:
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        self.fail("Uvicorn 启动失败：" + log_path.read_text(encoding="utf-8"))
                    try:
                        if request("GET", "/docs")[0] == 200:
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    if time.monotonic() >= deadline:
                        self.fail("Uvicorn 未在等待期限内启动")
                    time.sleep(0.05)

                self.assertEqual(request("GET", "/api/inventory")[0], 401)
                status, logged_in, _ = request("POST", "/api/auth/login", {"username": "admin", "password": PASSWORD})
                self.assertEqual(status, 200)
                admin = "Bearer " + logged_in["data"]["access_token"]
                identities = {}
                for role in ("staff", "viewer"):
                    self.assertEqual(request("POST", "/api/users", {"username": role, "password": PASSWORD, "role": role}, admin)[0], 201)
                    status, login, _ = request("POST", "/api/auth/login", {"username": role, "password": PASSWORD})
                    self.assertEqual(status, 200)
                    identities[role] = "Bearer " + login["data"]["access_token"]
                expires = (business_today() + timedelta(days=365)).isoformat()
                status, registered, _ = request("POST", "/api/medicines", {"name": "虚构HTTP药品", "location": "虚构CT室", "expires_on": expires}, admin)
                self.assertEqual(status, 201)
                medicine = registered["data"]
                before_scan = db.path.read_bytes()
                self.assertEqual(request("POST", "/api/nfc/scan", {"nfc_uuid": medicine["nfc_uuid"]}, identities["staff"])[0], 200)
                status, simulated, _ = request("POST", "/api/nfc/simulate", {"name": "虚构HTTP药品"}, identities["viewer"])
                self.assertEqual(status, 200)
                self.assertEqual(simulated["data"]["nfc_uuid"], medicine["nfc_uuid"])
                self.assertEqual(db.path.read_bytes(), before_scan)
                self.assertEqual(request("POST", "/api/nfc/dispense", {key: medicine[key] for key in ("nfc_uuid", "name", "location", "expires_on")}, identities["viewer"])[0], 403)
                self.assertEqual(request("POST", "/api/nfc/dispense", {key: medicine[key] for key in ("nfc_uuid", "name", "location", "expires_on")}, identities["staff"])[0], 200)
                self.assertEqual(request("POST", "/api/nfc/dispense", {key: medicine[key] for key in ("nfc_uuid", "name", "location", "expires_on")}, identities["staff"])[0], 409)
                status, inventory, _ = request("GET", "/api/inventory", authorization=admin)
                self.assertEqual(status, 200)
                self.assertEqual(inventory["data"][0], {"name": "虚构HTTP药品", "stock": 0, "available_stock": 0})
                status, logs, _ = request("GET", "/api/logs?action=dispense", authorization=admin)
                self.assertEqual(status, 200)
                self.assertEqual(len(logs["data"]), 1)
                self.assertEqual(logs["data"][0]["quantity"], 1)
                self.assertEqual(logs["data"][0]["medicine_id"], medicine["id"])
                before_export = db.path.read_bytes()
                status, download, headers = request("GET", "/api/export/medicines", authorization=admin)
                self.assertEqual(status, 200)
                self.assertTrue(download.startswith(codecs.BOM_UTF8))
                self.assertIn("attachment", headers["Content-Disposition"])
                self.assertEqual(next(csv.DictReader(io.StringIO(download.decode("utf-8-sig"))))["status"], "dispensed")
                self.assertEqual(db.path.read_bytes(), before_export)
                self.assertEqual(request("GET", "/api/medicines/not-an-id", authorization=admin)[0], 422)
                self.assertEqual(request("GET", "/api/auth/me", authorization=admin)[0], 200)
                # 批量请求也经过真实服务器和网络，不改变上面的原有单支验收。
                batch_body = {"name": "虚构HTTP批量药品", "location": "虚构批量柜", "expires_on": expires, "quantity": 10}
                before_batch = db.path.read_bytes()
                for role in ("staff", "viewer"):
                    self.assertEqual(request("POST", "/api/medicines/batch", batch_body, identities[role])[0], 403)
                self.assertEqual(request("POST", "/api/medicines/batch", dict(batch_body, quantity=True), admin)[0], 422)
                self.assertEqual(db.path.read_bytes(), before_batch)
                status, batch, _ = request("POST", "/api/medicines/batch", batch_body, admin)
                self.assertEqual(status, 201)
                self.assertEqual(batch["data"]["created_count"], 10)
                batch_rows = batch["data"]["medicines"]
                self.assertEqual(len({row["id"] for row in batch_rows}), 10)
                self.assertEqual(len({row["nfc_uuid"] for row in batch_rows}), 10)
                status, registered_logs, _ = request("GET", "/api/logs?action=register", authorization=admin)
                self.assertEqual(status, 200)
                batch_ids = {row["id"] for row in batch_rows}
                self.assertEqual(len([row for row in registered_logs["data"] if row["medicine_id"] in batch_ids and row["quantity"] == 1]), 10)
                status, scanned, _ = request("POST", "/api/nfc/simulate", {"name": batch_body["name"]}, identities["staff"])
                self.assertEqual(status, 200)
                confirmed = {key: scanned["data"][key] for key in ("nfc_uuid", "name", "location", "expires_on")}
                self.assertEqual(request("POST", "/api/nfc/dispense", confirmed, identities["staff"])[0], 200)
                self.assertEqual(request("POST", "/api/nfc/dispense", confirmed, identities["staff"])[0], 409)
                status, dashboard, _ = request("GET", "/api/dashboard", authorization=identities["viewer"])
                self.assertEqual(status, 200)
                self.assertEqual((dashboard["data"]["registered_count"], dashboard["data"]["dispensed_count"]), (11, 2))
                self.assertEqual(next(row for row in dashboard["data"]["inventory"] if row["name"] == batch_body["name"])["stock"], 9)
                self.assertIsNone(process.poll())
                print("HTTP_SMOKE: 登录200 登记201 扫描200 模拟200 viewer领取403 首次领取200 重复409 日志1条 CSV200")
                print("BATCH_HTTP: 管理员201、staff/viewer403、独立UUID10个、登记日志10条、模拟及重复领取通过")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.assertIsNotNone(process.returncode)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="medicine-cli-test-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "test.sqlite3"

    def test_explicit_init_command_uses_only_requested_temporary_database(self):
        response = subprocess.run([sys.executable, "-B", "-m", "backend.cli", "--db-path", str(self.path), "init-db"],
                                  cwd=BASE_DIR, capture_output=True, text=True, timeout=10)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertTrue(self.path.is_file())
        self.assertIn("没有创建默认账号", response.stdout)

    def test_create_admin_uses_hidden_password_and_blocks_second_bootstrap(self):
        db = MedicineDB(self.path)
        db.init_db()
        arguments = ["cli", "--db-path", str(self.path), "create-admin", "--username", "admin"]
        output = io.StringIO()
        with patch.object(sys, "argv", arguments), patch.object(cli.getpass, "getpass", side_effect=[PASSWORD, PASSWORD]) as prompt:
            with contextlib.redirect_stdout(output):
                result = cli.main()
        self.assertEqual(result, 0)
        self.assertEqual(prompt.call_count, 2)
        self.assertIsNotNone(db.login_user("admin", PASSWORD))
        self.assertNotIn(PASSWORD, output.getvalue())
        with patch.object(sys, "argv", arguments), patch.object(cli.getpass, "getpass", side_effect=[PASSWORD, PASSWORD]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(), 1)

    def test_password_mismatch_does_not_create_account(self):
        db = MedicineDB(self.path)
        db.init_db()
        arguments = ["cli", "--db-path", str(self.path), "create-admin", "--username", "admin"]
        with patch.object(sys, "argv", arguments), patch.object(cli.getpass, "getpass", side_effect=[PASSWORD, "different"]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(), 1)
        self.assertIsNone(db.login_user("admin", PASSWORD))


if __name__ == "__main__":
    unittest.main(verbosity=2)
