"""操作日志CSV回归：只使用临时SQLite、临时附件目录和虚构账号。"""
import asyncio
import codecs
import csv
import io
import json
import secrets
import sqlite3
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import data_io
from backend import api
from backend.auth import create_access_token
from backend.config import Settings
from backend.main import create_app
from medicine_db import MedicineDB


class LogExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="log-export-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = MedicineDB(self.root / "test.sqlite3")
        self.db.init_db()
        self.users = {"admin": self.db.create_first_admin("虚构管理员", "Fictional-test-password-2026")}
        for role in ("staff", "viewer"):
            self.users[role] = self.db.create_user(self.users["admin"]["id"], "虚构" + role, "Fictional-test-password-2026", role)
        self.settings = Settings(self.db.path, secrets.token_urlsafe(48))
        self.client = self.enterContext(TestClient(create_app(self.settings, self.db), raise_server_exceptions=False))
        self.headers = {role: {"Authorization": "Bearer " + create_access_token(row["id"], self.settings)["access_token"]} for role, row in self.users.items()}

    def medicine(self, name="虚构肾上腺素"):
        return self.db.add_medicine(name, "2035-12-31", self.users["admin"]["id"], "虚构药品柜")

    def mixed(self):
        row = self.medicine()
        self.db.withdraw_by_nfc(row["nfc_uuid"], self.users["staff"]["id"])
        return row

    def get(self, role="admin", **kwargs):
        return self.client.get("/api/export/logs", headers=self.headers[role], **kwargs)

    def rows(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_admin_exports_all_original_logs(self):
        self.mixed()
        rows = self.rows(self.get())
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["操作用户ID"] for r in rows}, {str(self.users["admin"]["id"]), str(self.users["staff"]["id"])})

    def test_staff_exports_only_own_logs(self):
        self.mixed()
        rows = self.rows(self.get("staff"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["操作用户ID"], str(self.users["staff"]["id"]))
        self.assertEqual(rows[0]["操作类型"], "dispense")

    def test_viewer_is_forbidden(self):
        self.assertEqual(self.get("viewer").status_code, 403)

    def test_missing_login_is_unauthorized(self):
        self.assertEqual(self.client.get("/api/export/logs").status_code, 401)

    def test_scope_cannot_be_overridden_by_client_parameters(self):
        self.mixed()
        for params in ({"user_id": self.users["admin"]["id"]}, {"role": "admin"}, {"action": "register"}, {"output_path": "/private/file"}):
            with self.subTest(params=params):
                response = self.get("staff", params=params)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("虚构管理员", response.text)

    def test_headers_encoding_uuid_fields_and_business_filename(self):
        medicine = self.medicine()
        response = self.get()
        rows = self.rows(response)
        self.assertTrue(response.content.startswith(codecs.BOM_UTF8))
        self.assertEqual(tuple(rows[0]), data_io.LOG_CSV_FIELDS)
        self.assertEqual(rows[0]["药品名称（当前）"], medicine["name"])
        self.assertEqual(rows[0]["NFC UUID（完整）"], medicine["nfc_uuid"])
        self.assertEqual(rows[0]["操作数量"], "1")
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("filename*=utf-8''", response.headers["content-disposition"])
        from urllib.parse import unquote
        filename = unquote(response.headers["content-disposition"].split("''", 1)[1])
        self.assertRegex(filename, r"^操作日志_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.csv$")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertNotIn(str(self.root), str(response.headers))

    def test_beijing_date_rollover_and_original_utc_preserved(self):
        self.medicine()
        with self.db.connection(write=True) as connection:
            connection.execute("UPDATE operation_logs SET created_at=?", ("2026-10-24 20:30:45",))
        row = self.rows(self.get())[0]
        self.assertEqual(row["操作时间（北京时间）"], "2026-10-25 04:30:45")
        self.assertEqual(row["操作时间（原始UTC）"], "2026-10-24 20:30:45")

    def test_invalid_legacy_timestamp_is_not_fabricated(self):
        self.medicine()
        with self.db.connection(write=True) as connection:
            connection.execute("UPDATE operation_logs SET created_at=?", ("原始时间异常",))
        row = self.rows(self.get())[0]
        self.assertEqual(row["操作时间（原始UTC）"], "原始时间异常")
        self.assertEqual(row["操作时间（北京时间）"], "")

    def test_25_batch_records_all_export_with_independent_ids(self):
        self.db.add_medicines_batch("虚构批量药品", "2035-12-31", 25, self.users["admin"]["id"])
        rows = self.rows(self.get())
        self.assertEqual(len(rows), 25)
        self.assertEqual(len({r["日志ID"] for r in rows}), 25)
        self.assertEqual(len({r["药品ID"] for r in rows}), 25)
        self.assertEqual(len({r["NFC UUID（完整）"] for r in rows}), 25)

    def test_empty_admin_and_staff_exports_have_only_header(self):
        for role in ("admin", "staff"):
            response = self.get(role)
            self.assertEqual(self.rows(response), [])
            self.assertEqual(next(csv.reader(io.StringIO(response.content.decode("utf-8-sig")))), list(data_io.LOG_CSV_FIELDS))

    def test_current_medicine_name_is_explicit_and_raw_action_preserved(self):
        row = self.medicine()
        self.db.update_medicine_details(row["id"], "虚构已更名药品", "虚构位置", "2035-12-31", self.users["admin"]["id"])
        exported = self.rows(self.get())[0]
        self.assertEqual(exported["药品名称（当前）"], "虚构已更名药品")
        self.assertEqual(exported["操作类型"], "register")
        self.assertEqual(exported["药品ID"], str(row["id"]))

    def test_time_descending_and_id_tie_breaker(self):
        self.db.add_medicines_batch("虚构排序药品", "2035-12-31", 3, self.users["admin"]["id"])
        with self.db.connection(write=True) as connection:
            connection.execute("UPDATE operation_logs SET created_at=? WHERE id IN (1,3)", ("2026-10-24 10:00:00",))
            connection.execute("UPDATE operation_logs SET created_at=? WHERE id=2", ("2026-10-25 10:00:00",))
        self.assertEqual([r["日志ID"] for r in self.rows(self.get())], ["2", "3", "1"])

    def test_formula_injection_protection_does_not_change_database(self):
        medicine = self.medicine('=HYPERLINK("https://invalid.example")')
        with self.db.connection(write=True) as connection:
            connection.execute("UPDATE users SET username=? WHERE id=?", ("+虚构管理员", self.users["admin"]["id"]))
        before = self.db.path.read_bytes()
        row = self.rows(self.get())[0]
        self.assertEqual(row["药品名称（当前）"], "'" + medicine["name"])
        self.assertEqual(row["操作用户名"], "'+虚构管理员")
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_export_leaves_database_and_logs_byte_identical(self):
        self.mixed()
        before = self.db.path.read_bytes()
        original_logs = self.db.get_operation_logs(self.users["admin"]["id"])
        self.rows(self.get())
        self.rows(self.get("staff"))
        self.assertEqual(self.db.path.read_bytes(), before)
        self.assertEqual(self.db.get_operation_logs(self.users["admin"]["id"]), original_logs)

    def test_large_export_iterates_without_materializing_all_logs(self):
        row = self.medicine()
        with self.db.connection(write=True) as connection:
            connection.executemany(self.db.queries["add_operation_log"], ({"user_id": self.users["admin"]["id"], "medicine_id": row["id"], "action": "enable", "quantity": 1} for _ in range(20000)))
        tracemalloc.start()
        try:
            with patch.object(self.db, "get_operation_logs", side_effect=AssertionError("不得载入完整日志列表")):
                result = data_io.export_operation_logs_csv(self.db, self.users["admin"]["id"], self.root / "large.csv")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result["record_count"], 20001)
        self.assertLess(peak, 4 * 1024 * 1024, f"导出Python峰值内存过大：{peak}")
        with Path(result["path"]).open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual(sum(1 for _ in csv.DictReader(f)), 20001)

    def test_iterator_and_existing_list_query_share_permission_and_filters(self):
        self.mixed()
        for role in ("admin", "staff"):
            user_id = self.users[role]["id"]
            with self.db.iter_operation_logs(user_id) as rows:
                self.assertEqual(list(rows), self.db.get_operation_logs(user_id))
        with self.assertRaises(PermissionError):
            with self.db.iter_operation_logs(self.users["viewer"]["id"]):
                self.fail("viewer不能进入读取上下文")

    def test_iterator_early_exit_closes_connection(self):
        self.medicine()
        connections = []
        original = self.db._connect
        def connect(*args, **kwargs):
            value = original(*args, **kwargs); connections.append(value); return value
        with patch.object(self.db, "_connect", side_effect=connect):
            with self.db.iter_operation_logs(self.users["admin"]["id"]) as rows:
                next(rows)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    def test_function_refuses_overwrite_and_invalid_output_directory(self):
        self.medicine()
        target = self.root / "keep.csv"; target.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            data_io.export_operation_logs_csv(self.db, self.users["admin"]["id"], target)
        self.assertEqual(target.read_bytes(), b"keep")
        with self.assertRaises(FileNotFoundError):
            data_io.export_operation_logs_csv(self.db, self.users["admin"]["id"], self.root / "missing/logs.csv")

    def test_write_failure_cleans_file_and_read_connection(self):
        self.medicine()
        before = self.db.path.read_bytes()
        with patch.object(data_io.os, "fsync", side_effect=OSError("虚构写入失败")):
            with self.assertRaises(OSError):
                data_io.export_operation_logs_csv(self.db, self.users["admin"]["id"], self.root / "broken.csv")
        self.assertFalse((self.root / "broken.csv").exists())
        self.assertFalse(list(self.root.glob(".medicine-csv-*")))
        self.assertEqual(self.db.path.read_bytes(), before)
        self.db.add_medicine("失败后正常登记（虚构）", "2035-12-31", self.users["admin"]["id"])

    def test_http_failure_cleans_directory_and_never_leaks_internal_paths(self):
        created = []; original = tempfile.TemporaryDirectory
        def directory(*args, **kwargs):
            value = original(prefix="csv-", dir=self.root); created.append(Path(value.name)); return value
        with patch.object(api.tempfile, "TemporaryDirectory", side_effect=directory):
            with patch.object(data_io, "export_operation_logs_csv", side_effect=OSError(str(self.root))):
                response = self.get()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "export_failed")
        self.assertNotIn(str(self.root), response.text)
        self.assertTrue(created and all(not p.exists() for p in created))

    def test_download_cleanup_after_success(self):
        created = []; original = tempfile.TemporaryDirectory
        def directory(*args, **kwargs):
            value = original(prefix="csv-", dir=self.root); created.append(Path(value.name)); return value
        with patch.object(api.tempfile, "TemporaryDirectory", side_effect=directory):
            self.rows(self.get())
        self.assertTrue(created and all(not p.exists() for p in created))

    def test_send_failure_also_removes_log_csv(self):
        temporary = tempfile.TemporaryDirectory(prefix="csv-send-", dir=self.root)
        path = Path(temporary.name) / "logs.csv"; path.write_text("id\n")
        response = api.TemporaryCSVResponse(path, temporary, filename="操作日志_2026-10-24_12-00-00.csv")
        async def send(message):
            if message["type"] == "http.response.body": raise OSError("虚构网络断开")
        async def receive(): return {"type": "http.request", "body": b"", "more_body": False}
        with self.assertRaises(OSError):
            asyncio.run(response({"type": "http", "method": "GET", "headers": [], "extensions": {},
                                  "asgi": {"spec_version": "2.4"}}, receive, send))
        self.assertFalse(path.parent.exists())

    def test_range_download_rejected_before_file_generation(self):
        with patch.object(data_io, "export_operation_logs_csv", side_effect=AssertionError("不应导出")):
            response = self.client.get("/api/export/logs", headers={**self.headers["admin"], "Range": "bytes=0-10"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_range")

    def test_role_is_rechecked_at_database_read_not_trusted_from_http(self):
        original = data_io.export_operation_logs_csv
        def revoked(db, user_id, output):
            with db.connection(write=True) as connection:
                connection.execute("UPDATE users SET role=? WHERE id=?", ("viewer", user_id))
            return original(db, user_id, output)
        with patch.object(data_io, "export_operation_logs_csv", side_effect=revoked):
            self.assertEqual(self.get().status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
