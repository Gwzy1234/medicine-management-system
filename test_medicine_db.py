"""第三阶段标准库自动测试：python3 -B -m unittest -v test_medicine_db。

每项数据库测试使用独立 TemporaryDirectory，不连接默认业务数据库。
日期固定在测试对象中，不依赖执行当天。故障注入只作用于测试对象。
"""

import ast
import hashlib
import importlib.util
import re
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import medicine_db as m


TODAY = date(2026, 10, 22)
PASSWORD = "Course-test-password-2026"


class SQLLoaderTests(unittest.TestCase):
    def load_text(self, text):
        with tempfile.TemporaryDirectory(prefix="medicine-loader-test-") as tmp:
            path = Path(tmp) / "queries.sql"
            path.write_text(text, encoding="utf-8")
            return m.load_queries(path)

    def test_load_all_29_queries_and_exact_parameters(self):
        queries = m.load_queries()
        expected = {
            "create_user": {"username", "password_hash", "role"},
            "get_user_by_username": {"username"},
            "get_user_by_id": {"user_id"},
            "get_user_role": {"user_id"},
            "get_users": set(),
            "add_medicine": {"nfc_uuid", "name", "location", "expires_on"},
            "get_medicine_by_id": {"medicine_id"},
            "get_medicine_by_nfc": {"nfc_uuid"},
            "get_medicines": set(),
            "get_medicines_by_name": {"name"},
            "get_in_stock_medicines": set(),
            "get_dispensed_medicines": set(),
            "get_expired_medicines": {"today"},
            "get_expiring_medicines": {"today", "deadline"},
            "get_disabled_medicines": set(),
            "enable_medicine": {"medicine_id"},
            "disable_medicine": {"medicine_id"},
            "get_book_stock_by_name": {"name"},
            "get_available_stock_by_name": {"name", "today"},
            "get_stock_by_name": {"today"},
            "get_low_stock_medicines": {"stock_threshold"},
            "get_dispensable_medicine_by_nfc": {"nfc_uuid", "today"},
            "dispense_medicine_by_nfc": {"nfc_uuid", "today"},
            "add_operation_log": {"user_id", "medicine_id", "action", "quantity"},
            "get_operation_logs": set(),
            "get_operation_logs_by_user": {"user_id"},
            "get_operation_logs_by_medicine": {"medicine_id"},
            "get_operation_logs_by_action": {"action"},
            "update_medicine_details": {"medicine_id", "name", "location", "expires_on"},
        }
        self.assertEqual(len(queries), 29)
        self.assertEqual(set(queries), set(expected))
        for name, parameters in expected.items():
            with self.subTest(query=name):
                actual = set(re.findall(r":([a-z_]+)", m._without_comments(queries[name])))
                self.assertEqual(actual, parameters)

    def test_loader_preserves_semicolons_and_comment_markers_in_strings(self):
        result = self.load_text("-- preamble\n-- name: example\nSELECT '--;/*文字*/'; -- 尾注\n")
        connection = sqlite3.connect(":memory:")
        try:
            self.assertEqual(connection.execute(result["example"]).fetchone()[0], "--;/*文字*/")
        finally:
            connection.close()

    def test_loader_rejects_duplicate_empty_malformed_and_multiple_statements(self):
        samples = [
            "-- comments only", "SELECT 1;", "-- name:\nSELECT 1;",
            "-- name: bad-name\nSELECT 1;", "-- name: empty\n-- 注释",
            "-- name: first\nSELECT 1;\n-- name: first\nSELECT 2;",
            "-- name: two\nSELECT 1; SELECT 2;", "-- name: incomplete\nSELECT 1",
            "-- name: trailing\nSELECT 1; SELECT 2",
        ]
        for text in samples:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load_text(text)

    def test_import_and_construction_do_not_open_or_modify_database(self):
        with tempfile.TemporaryDirectory(prefix="medicine-import-test-") as tmp:
            target = Path(tmp) / "not-created.db"
            with patch.object(sqlite3, "connect", side_effect=AssertionError("不应连接数据库")):
                spec = importlib.util.spec_from_file_location("phase3_import_check", m.__file__)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                database = module.MedicineDB(target)
            self.assertEqual(database.path, target)
            self.assertFalse(target.exists())

    def test_original_sql_hashes_unchanged(self):
        expected = {
            m.SCHEMA_PATH: "570b84f8d6ecded1e68e797c68baaf536ff82f8a37856c26d5ee9b0b4e9dedc0",
            m.QUERIES_PATH: "ec4c213ea538c8a9eac7b35321a9220526f2574568759d20e7b1763d587c3d2a",
        }
        for path, digest in expected.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_python_business_calls_use_named_queries_and_bound_parameters(self):
        tree = ast.parse(Path(m.__file__).read_text(encoding="utf-8"))
        names = set(m.load_queries())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "_execute" and len(node.args) >= 2:
                    argument = node.args[1]
                    if isinstance(argument, ast.Constant):
                        self.assertIn(argument.value, names)
                if node.func.attr == "execute" and node.args and isinstance(node.args[0], ast.Constant):
                    sql = node.args[0].value
                    if isinstance(sql, str):
                        # Python 中只能内嵌事务/连接/元数据 SQL，不能另写业务 DML。
                        self.assertNotRegex(sql, r"(?i)\b(?:INSERT|UPDATE|DELETE)\b")
                        if "SELECT" in sql:
                            self.assertIn("sqlite_master", sql)
        source = Path(m.__file__).read_text(encoding="utf-8")
        for old_name in ("medicine_batches", "nfc_tags", "batch_id", "batch_no", "min_stock",
                         "equipment", "decrement_batch", "out_medicine", "restock_medicine"):
            self.assertNotIn(old_name, source)
        self.assertFalse(hasattr(m.MedicineDB, "get_low_stock_medicines"))


class PasswordTests(unittest.TestCase):
    def test_hash_contains_algorithm_iterations_salt_and_digest(self):
        encoded = m.hash_password(PASSWORD)
        algorithm, iterations, salt, digest = encoded.split("$")
        self.assertEqual(algorithm, "pbkdf2_sha256")
        self.assertEqual(int(iterations), m.HASH_ITERATIONS)
        self.assertEqual(len(bytes.fromhex(salt)), 16)
        self.assertEqual(len(bytes.fromhex(digest)), 32)
        self.assertNotIn(PASSWORD, encoded)
        self.assertTrue(m.verify_password(PASSWORD, encoded))
        self.assertFalse(m.verify_password("Wrong-password", encoded))

    def test_equal_passwords_get_different_random_salts(self):
        self.assertNotEqual(m.hash_password(PASSWORD), m.hash_password(PASSWORD))

    def test_bad_password_input_is_rejected(self):
        for value in (None, 123, "", "short", "x" * 1025):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                m.hash_password(value)
        for value in (None, 123, "", "short", "x" * 1025):
            self.assertFalse(m.verify_password(value, "invalid"))

    def test_plaintext_and_damaged_hashes_are_not_accepted(self):
        for encoded in (None, PASSWORD, "bad$1$x$x", "pbkdf2_sha256$999999999$x$x",
                        "pbkdf2_sha256$600000$nothex$abc", "pbkdf2_sha256$600000$00$00"):
            with self.subTest(encoded=encoded):
                self.assertFalse(m.verify_password(PASSWORD, encoded))


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="medicine-init-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "medical.db"
        self.db = m.MedicineDB(self.path, today_provider=lambda: TODAY)

    def test_initialize_only_three_business_tables(self):
        self.db.init_db()
        with self.db.connection() as connection:
            names = {r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            self.assertEqual(names, {"users", "medicines", "operation_logs"})
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_reinitialize_preserves_existing_data(self):
        self.db.init_db()
        admin = self.db.create_first_admin("admin", PASSWORD)
        medicine = self.db.add_medicine("肾上腺素", "2030-01-01", admin["id"])
        self.db.init_db()
        self.assertEqual(self.db.login_user("admin", PASSWORD), admin)
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], admin["id"]), medicine)

    def test_wrong_schema_and_empty_existing_file_are_not_rebuilt(self):
        for content in (b"", b"not a database"):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(m.DatabaseInitializationError):
                    self.db.init_db()
                self.assertEqual(self.path.read_bytes(), content)
        self.path.unlink()  # 仅删除本测试建立的临时文件。
        connection = sqlite3.connect(self.path)
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
        connection.commit()
        connection.close()
        before = self.path.read_bytes()
        with self.assertRaises(m.DatabaseInitializationError):
            self.db.init_db()
        self.assertEqual(self.path.read_bytes(), before)

    def test_reading_uninitialized_database_does_not_create_it(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.db.get_medicines(1)
        self.assertFalse(self.path.exists())

    def test_failed_initialization_does_not_publish_partial_database(self):
        with patch.object(m.os, "link", side_effect=OSError("测试发布失败")):
            with self.assertRaises(OSError):
                self.db.init_db()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_connections_enable_foreign_keys_and_close_on_success_and_error(self):
        self.db.init_db()
        for write in (False, True):
            with self.db.connection(write=write) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        with self.assertRaisesRegex(RuntimeError, "故障"):
            with self.db.connection(write=True) as broken:
                raise RuntimeError("故障")
        with self.assertRaises(sqlite3.ProgrammingError):
            broken.execute("SELECT 1")

    def test_two_simultaneous_initializations_are_safe(self):
        gate = threading.Barrier(2)
        def initialize(_):
            database = m.MedicineDB(self.path)
            gate.wait(timeout=10)
            database.init_db()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(initialize, range(2)))
        self.db.init_db()

    def test_default_paths_are_project_relative_and_timeout_is_validated(self):
        self.assertEqual(m.MedicineDB().path, m.BASE_DIR / "medical.db")
        self.assertEqual(m.MedicineDB("relative.sqlite3").path, m.BASE_DIR / "relative.sqlite3")
        for value in (-1, float("inf"), float("nan"), True, "5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.MedicineDB(self.path, timeout=value)


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="medicine-business-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "medical.db"
        self.db = m.MedicineDB(self.path, today_provider=lambda: TODAY)
        self.db.init_db()
        self.admin = self.db.create_first_admin("admin", PASSWORD)["id"]
        self.staff = self.db.create_user(self.admin, "staff", PASSWORD, "staff")["id"]
        self.viewer = self.db.create_user(self.admin, "viewer", PASSWORD, "viewer")["id"]

    def add(self, *, name="肾上腺素", expires="2030-01-01"):
        return self.db.add_medicine(name, expires, self.admin)

    def logs(self, **filters):
        return self.db.get_operation_logs(self.admin, **filters)

    def stock(self, name="肾上腺素"):
        result = self.db.get_stock_for_name(name, self.viewer)
        return result["stock"], result["available_stock"]

    def fail_log(self, callback):
        """真正执行一个违反 CHECK 的日志 INSERT，让 SQLite 抛错并回滚。"""
        original = self.db._execute
        def execute(connection, name, parameters=None):
            if name == "add_operation_log":
                parameters = dict(parameters, quantity=0)
            return original(connection, name, parameters)
        with patch.object(self.db, "_execute", side_effect=execute):
            with self.assertRaises(sqlite3.IntegrityError):
                callback()

    def test_first_admin_and_repeated_bootstrap_protection(self):
        self.assertEqual(self.db.login_user("admin", PASSWORD)["role"], "admin")
        with self.assertRaises(m.AlreadyInitializedError):
            self.db.create_first_admin("second_admin", PASSWORD)
        self.assertEqual(len(self.db.get_users(self.admin)), 3)

    def test_login_success_wrong_password_and_unknown_username(self):
        self.assertEqual(self.db.login_user("staff", PASSWORD), {
            "id": self.staff, "username": "staff", "role": "staff",
        })
        for username, password in (("staff", "Wrong-password"), ("missing", PASSWORD),
                                   (None, PASSWORD), ("staff", None)):
            self.assertIsNone(self.db.login_user(username, password))

    def test_password_is_encoded_and_public_user_results_never_contain_it(self):
        with self.db.connection() as connection:
            row = self.db._execute(connection, "get_user_by_username", {"username": "admin"}).fetchone()
            self.assertTrue(m.verify_password(PASSWORD, row["password"]))
            self.assertNotEqual(row["password"], PASSWORD)
        results = self.db.get_users(self.admin)
        results += [self.db.get_user_by_id(self.staff, self.staff), self.db.login_user("viewer", PASSWORD)]
        self.assertTrue(all(set(row) == {"id", "username", "role"} for row in results))

    def test_user_lookup_and_user_management_permissions(self):
        self.assertEqual(self.db.get_user_by_id(self.staff, self.admin)["username"], "staff")
        self.assertIsNone(self.db.get_user_by_id(999999, self.admin))
        for user in (self.staff, self.viewer):
            with self.subTest(user=user):
                with self.assertRaises(PermissionError):
                    self.db.create_user(user, "illegal", PASSWORD)
                with self.assertRaises(PermissionError):
                    self.db.get_users(user)
                with self.assertRaises(PermissionError):
                    self.db.get_user_by_id(self.admin, user)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_user(self.admin, "staff", PASSWORD)
        with self.assertRaises(ValueError):
            self.db.create_user(self.admin, "bad", PASSWORD, "owner")

    def test_unknown_user_and_boolean_ids_cannot_bypass_permissions(self):
        for call in (lambda: self.db.get_medicines(999999),
                     lambda: self.db.add_medicine("药品", "2030-01-01", 999999),
                     lambda: self.db.withdraw_by_nfc(str(uuid4()), 999999)):
            with self.assertRaises(PermissionError):
                call()
        for user in (True, 0, -1, "1"):
            with self.subTest(user=user), self.assertRaises(ValueError):
                self.db.get_medicines(user)

    def test_all_roles_can_query_medicines_stock_and_nfc(self):
        medicine = self.add()
        for user in (self.admin, self.staff, self.viewer):
            with self.subTest(user=user):
                self.assertEqual(len(self.db.get_medicines(user)), 1)
                self.assertEqual(self.db.get_medicine_by_id(medicine["id"], user), medicine)
                self.assertEqual(self.db.get_medicine_by_nfc(medicine["nfc_uuid"], user), medicine)
                self.assertEqual(self.db.get_stock_for_name("肾上腺素", user)["stock"], 1)
                self.assertEqual(self.db.check_permission(user), self.db.get_user_by_id(user, user)["role"])

    def test_register_single_medicine_with_uuid_defaults_and_log(self):
        medicine = self.add()
        self.assertEqual(UUID(medicine["nfc_uuid"]).version, 4)
        self.assertEqual(medicine["status"], "in_stock")
        self.assertEqual(medicine["is_active"], 1)
        self.assertEqual(medicine["location"], "未指定")
        self.assertEqual(self.stock(), (1, 1))
        log, = self.logs()
        self.assertEqual((log["user_id"], log["medicine_id"], log["action"], log["quantity"]),
                         (self.admin, medicine["id"], "register", 1))
        self.assertEqual(log["nfc_uuid"], medicine["nfc_uuid"])
        datetime.strptime(log["created_at"], "%Y-%m-%d %H:%M:%S")

    def test_same_name_independent_uuids_and_expiries_count_as_units(self):
        first = self.add(expires="2029-01-01")
        second = self.add(expires="2031-01-01")
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["nfc_uuid"], second["nfc_uuid"])
        self.assertEqual([r["expires_on"] for r in self.db.get_medicines_by_name("肾上腺素", self.viewer)],
                         ["2029-01-01", "2031-01-01"])
        self.assertEqual(self.stock(), (2, 2))

    def test_only_admin_can_register_or_enable_disable(self):
        medicine = self.add()
        for user in (self.staff, self.viewer):
            for call in (lambda: self.db.add_medicine("阿托品", "2030-01-01", user),
                         lambda: self.db.disable_medicine(medicine["id"], user),
                         lambda: self.db.enable_medicine(medicine["id"], user)):
                with self.subTest(user=user), self.assertRaises(PermissionError):
                    call()
        self.assertEqual(self.stock(), (1, 1))
        self.assertEqual(len(self.logs()), 1)

    def test_validation_rejects_empty_names_locations_and_invalid_dates(self):
        for name, expiry, location in ((" ", "2030-01-01", "柜"), (None, "2030-01-01", "柜"),
                                       ("药品", None, "柜"), ("药品", "2030-1-01", "柜"),
                                       ("药品", "2030-02-30", "柜"), ("药品", "20300101", "柜"),
                                       ("药品", "2030-01-01", " ")):
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                self.db.add_medicine(name, expiry, self.admin, location)
        self.assertEqual(self.db.get_medicines(self.admin), [])
        self.assertEqual(self.logs(), [])

    def test_name_and_location_normalization_and_sql_injection_are_bound(self):
        medicine = self.db.add_medicine("  肾上腺素   注射液  ", "2030-01-01", self.admin, " CT室 ")
        self.assertEqual(medicine["name"], "肾上腺素 注射液")
        self.assertEqual(medicine["location"], "CT室")
        self.assertEqual(len(self.db.get_medicines_by_name(" 肾上腺素  注射液 ", self.viewer)), 1)
        attack = "x'; DROP TABLE users; --"
        self.db.add_medicine(attack, "2030-01-01", self.admin)
        self.assertEqual(len(self.db.get_medicines_by_name(attack, self.viewer)), 1)
        self.assertEqual(len(self.db.get_users(self.admin)), 3)

    def test_book_and_available_inventory_and_zero_inventory_names(self):
        valid = self.add()
        self.add(expires="2020-01-01")
        disabled = self.add()
        self.db.disable_medicine(disabled["id"], self.admin)
        self.assertEqual(self.stock(), (3, 1))
        self.db.withdraw_by_nfc(valid["nfc_uuid"], self.staff)
        self.assertEqual(self.stock(), (2, 0))
        another = self.add(name="阿托品")
        self.db.withdraw_by_nfc(another["nfc_uuid"], self.admin)
        stock = {row["name"]: (row["stock"], row["available_stock"])
                 for row in self.db.get_stock_by_name(self.viewer)}
        self.assertEqual(stock, {"肾上腺素": (2, 0), "阿托品": (0, 0)})
        self.assertEqual(self.stock("未登记"), (0, 0))

    def test_medicine_filters_and_expiry_deadline_boundaries(self):
        old = self.add(expires=(TODAY - timedelta(days=1)).isoformat())
        today = self.add(expires=TODAY.isoformat())
        boundary = self.add(expires=(TODAY + timedelta(days=30)).isoformat())
        self.add(expires=(TODAY + timedelta(days=31)).isoformat())
        self.db.disable_medicine(old["id"], self.admin)
        self.assertEqual([r["id"] for r in self.db.get_expired_medicines(self.viewer)], [old["id"]])
        self.assertEqual([r["id"] for r in self.db.get_expiring_medicines(self.viewer)], [today["id"], boundary["id"]])
        self.assertEqual([r["id"] for r in self.db.get_expiring_medicines(self.viewer, 0)], [today["id"]])
        self.assertEqual(len(self.db.get_in_stock_medicines(self.viewer)), 4)
        self.assertEqual(len(self.db.get_disabled_medicines(self.viewer)), 1)
        self.assertEqual(self.db.get_dispensed_medicines(self.viewer), [])
        for days in (-1, True, "30", 10**20):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.db.get_expiring_medicines(self.viewer, days)

    def test_first_withdraw_success_exact_log_and_inventory_decrement(self):
        medicine = self.add()
        result = self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)
        self.assertTrue(result["success"])
        self.assertEqual(result["reason"], "dispensed")
        self.assertEqual(result["medicine"]["status"], "dispensed")
        self.assertEqual(self.stock(), (0, 0))
        self.assertEqual(len(self.db.get_dispensed_medicines(self.viewer)), 1)
        log, = self.logs(action="dispense")
        self.assertEqual((log["user_id"], log["medicine_id"], log["quantity"]), (self.staff, medicine["id"], 1))

    def test_admin_also_can_withdraw(self):
        medicine = self.add()
        self.assertTrue(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.admin)["success"])

    def test_repeated_withdraw_cannot_succeed_or_add_log(self):
        medicine = self.add()
        self.assertTrue(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)["success"])
        for _ in range(3):
            result = self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)
            self.assertEqual(result, {"success": False, "reason": "already_dispensed", "medicine": None})
        self.assertEqual(self.stock(), (0, 0))
        self.assertEqual(len(self.logs(action="dispense")), 1)

    def test_unknown_uuid_scan_and_withdraw_never_register(self):
        token = str(uuid4())
        self.assertIsNone(self.db.get_medicine_by_nfc(token, self.viewer))
        self.assertEqual(self.db.withdraw_by_nfc(token, self.staff)["reason"], "not_found")
        self.assertIsNone(self.db.get_medicine_by_id(999999, self.viewer))
        self.assertEqual(self.db.get_medicines(self.admin), [])
        self.assertEqual(self.logs(), [])

    def test_expired_medicine_cannot_be_dispensed_and_stays_in_stock(self):
        medicine = self.add(expires=(TODAY - timedelta(days=1)).isoformat())
        self.assertEqual(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)["reason"], "expired")
        self.assertEqual(self.stock(), (1, 0))
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer)["status"], "in_stock")
        self.assertEqual(self.logs(action="dispense"), [])

    def test_disabled_medicine_cannot_be_dispensed(self):
        medicine = self.add()
        self.db.disable_medicine(medicine["id"], self.admin)
        self.assertEqual(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)["reason"], "disabled")
        self.assertEqual(self.stock(), (1, 0))
        self.assertEqual(self.logs(action="dispense"), [])

    def test_viewer_cannot_withdraw_even_with_a_valid_uuid(self):
        medicine = self.add()
        with self.assertRaises(PermissionError):
            self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.viewer)
        self.assertEqual(self.stock(), (1, 1))
        self.assertEqual(self.logs(action="dispense"), [])

    def test_expiry_today_can_be_dispensed(self):
        medicine = self.add(expires=TODAY.isoformat())
        self.assertEqual(self.stock(), (1, 1))
        self.assertTrue(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)["success"])

    def test_withdraw_log_failure_rolls_back_status_inventory_and_log(self):
        medicine = self.add()
        self.fail_log(lambda: self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff))
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer)["status"], "in_stock")
        self.assertEqual(self.stock(), (1, 1))
        self.assertEqual(self.logs(action="dispense"), [])
        self.assertTrue(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)["success"])

    def test_registration_log_failure_rolls_back_new_row_and_uuid(self):
        self.fail_log(self.add)
        self.assertEqual(self.db.get_medicines(self.admin), [])
        self.assertEqual(self.logs(), [])
        self.assertEqual(self.stock(), (0, 0))

    def test_duplicate_uuid_registration_does_not_change_inventory_or_logs(self):
        medicine = self.add()
        with patch.object(m, "uuid4", return_value=UUID(medicine["nfc_uuid"])):
            with self.assertRaises(sqlite3.IntegrityError):
                self.add()
        self.assertEqual(self.stock(), (1, 1))
        self.assertEqual(len(self.logs()), 1)

    def test_zero_row_update_rolls_back_without_dispense_log(self):
        medicine = self.add()
        original = self.db._execute
        class NoChange:
            rowcount = 0
        def execute(connection, name, parameters=None):
            if name == "dispense_medicine_by_nfc":
                return NoChange()
            return original(connection, name, parameters)
        with patch.object(self.db, "_execute", side_effect=execute):
            with self.assertRaises(RuntimeError):
                self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)
        self.assertEqual(self.stock(), (1, 1))
        self.assertEqual(self.logs(action="dispense"), [])

    def test_two_connections_concurrently_dispense_same_uuid_at_most_once(self):
        medicine = self.add()
        gate = threading.Barrier(2)
        def withdraw(_):
            database = m.MedicineDB(self.path, today_provider=lambda: TODAY)
            gate.wait(timeout=10)
            return database.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(withdraw, range(2)))
        self.assertEqual(sum(result["success"] for result in results), 1)
        self.assertEqual({result["reason"] for result in results}, {"dispensed", "already_dispensed"})
        self.assertEqual(self.stock(), (0, 0))
        self.assertEqual(len(self.logs(action="dispense")), 1)

    def test_parallel_first_admin_creation_only_one_succeeds(self):
        other = m.MedicineDB(Path(self.temp.name) / "bootstrap.db")
        other.init_db()
        gate = threading.Barrier(2)
        def bootstrap(index):
            gate.wait(timeout=10)
            try:
                return other.create_first_admin("first" + str(index), PASSWORD)
            except m.AlreadyInitializedError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(bootstrap, range(2)))
        successful = [result for result in results if result is not None]
        self.assertEqual(len(successful), 1)
        self.assertEqual(len(other.get_users(successful[0]["id"])), 1)

    def test_database_lock_timeout_is_an_error_never_success(self):
        medicine = self.add()
        impatient = m.MedicineDB(self.path, today_provider=lambda: TODAY, timeout=0.01)
        with self.db.connection(write=True):
            with self.assertRaises(sqlite3.OperationalError):
                impatient.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)
        self.assertEqual(self.stock(), (1, 1))
        self.assertEqual(self.logs(action="dispense"), [])

    def test_permission_and_today_are_checked_after_begin_immediate(self):
        medicine = self.add(expires=TODAY.isoformat())
        trace = []
        original_connect = self.db._connect
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(trace.append)
            return connection
        def today_provider():
            self.assertTrue(any(sql == "BEGIN IMMEDIATE" for sql in trace))
            self.assertTrue(any("SELECT role" in sql for sql in trace))
            return TODAY + timedelta(days=1)
        with patch.object(self.db, "_connect", side_effect=connect), patch.object(self.db, "today_provider", today_provider):
            result = self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)
        self.assertEqual(result["reason"], "expired")
        self.assertEqual(self.logs(action="dispense"), [])

    def test_enable_disable_logs_idempotence_and_independent_dispensed_status(self):
        medicine = self.add()
        self.assertTrue(self.db.disable_medicine(medicine["id"], self.admin)["changed"])
        self.assertFalse(self.db.disable_medicine(medicine["id"], self.admin)["changed"])
        self.assertEqual(self.stock(), (1, 0))
        self.assertTrue(self.db.enable_medicine(medicine["id"], self.admin)["changed"])
        self.assertFalse(self.db.enable_medicine(medicine["id"], self.admin)["changed"])
        self.assertEqual(self.stock(), (1, 1))
        self.assertEqual(len(self.logs(action="enable")), 1)
        self.assertEqual(len(self.logs(action="disable")), 1)
        self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)
        self.db.disable_medicine(medicine["id"], self.admin)
        result = self.db.enable_medicine(medicine["id"], self.admin)
        self.assertEqual(result["medicine"]["status"], "dispensed")
        self.assertEqual(self.stock(), (0, 0))
        self.assertFalse(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)["success"])

    def test_enable_and_disable_log_failure_roll_back_state(self):
        medicine = self.add()
        self.fail_log(lambda: self.db.disable_medicine(medicine["id"], self.admin))
        self.assertEqual(self.stock(), (1, 1))
        self.assertEqual(self.logs(action="disable"), [])
        self.db.disable_medicine(medicine["id"], self.admin)
        self.fail_log(lambda: self.db.enable_medicine(medicine["id"], self.admin))
        self.assertEqual(self.stock(), (1, 0))
        self.assertEqual(self.logs(action="enable"), [])

    def test_unknown_medicine_enable_disable_does_not_create_logs(self):
        for method in (self.db.enable_medicine, self.db.disable_medicine):
            with self.assertRaises(LookupError):
                method(999999, self.admin)
        self.assertEqual(self.logs(), [])

    def test_all_log_filters_and_combination_permissions(self):
        first = self.add()
        second = self.add(name="阿托品")
        self.db.withdraw_by_nfc(first["nfc_uuid"], self.staff)
        self.db.disable_medicine(second["id"], self.admin)
        self.assertEqual(len(self.logs()), 4)
        self.assertEqual(len(self.logs(filter_user_id=self.staff)), 1)
        self.assertEqual(len(self.logs(medicine_id=first["id"])), 2)
        self.assertEqual(len(self.logs(action="register")), 2)
        selected = self.logs(filter_user_id=self.staff, medicine_id=first["id"], action="dispense")
        self.assertEqual(len(selected), 1)
        self.assertEqual(self.logs(medicine_id=second["id"], action="dispense"), [])
        self.assertEqual(self.db.get_operation_logs(self.staff), selected)
        with self.assertRaises(PermissionError):
            self.db.get_operation_logs(self.staff, filter_user_id=self.admin)
        with self.assertRaises(PermissionError):
            self.db.get_operation_logs(self.viewer)
        with self.assertRaises(ValueError):
            self.logs(action="restock")

    def test_simulated_scan_uses_existing_uuid_then_the_same_withdraw_function(self):
        self.add(expires="2020-01-01")
        disabled = self.add(expires="2028-01-01")
        self.db.disable_medicine(disabled["id"], self.admin)
        eligible = self.add(expires="2029-01-01")
        self.add(expires="2030-01-01")
        before = self.stock()
        with patch.object(m, "uuid4", side_effect=AssertionError("扫描不得生成 UUID")):
            token = self.db.find_simulated_nfc_uuid("肾上腺素", self.staff)
            self.assertEqual(token, eligible["nfc_uuid"])
            self.assertEqual(self.stock(), before)
            self.assertTrue(self.db.withdraw_by_nfc(token, self.staff)["success"])
        self.assertEqual(len(self.db.get_medicines(self.viewer)), 4)
        self.assertEqual(len(self.logs(action="dispense")), 1)
        self.assertIsNone(self.db.find_simulated_nfc_uuid("不存在的药品", self.staff))

    def test_simulated_selection_does_not_bypass_viewer_or_reserve_medicine(self):
        medicine = self.add()
        token = self.db.find_simulated_nfc_uuid("肾上腺素", self.viewer)
        self.assertEqual(token, medicine["nfc_uuid"])
        with self.assertRaises(PermissionError):
            self.db.withdraw_by_nfc(token, self.viewer)
        self.db.withdraw_by_nfc(token, self.admin)
        self.assertEqual(self.db.withdraw_by_nfc(token, self.staff)["reason"], "already_dispensed")
        self.assertIsNone(self.db.find_simulated_nfc_uuid("肾上腺素", self.staff))

    def test_real_foreign_key_and_quantity_constraints(self):
        medicine = self.add()
        for user_id, medicine_id, action, quantity in (
            (999999, medicine["id"], "register", 1),
            (self.admin, 999999, "register", 1),
            (self.admin, medicine["id"], "dispense", 2),
            (self.admin, medicine["id"], "register", 0),
            (self.admin, medicine["id"], "register", 1.5),
        ):
            with self.subTest(action=action, quantity=quantity), self.assertRaises(sqlite3.IntegrityError):
                with self.db.connection(write=True) as connection:
                    self.db._execute(connection, "add_operation_log", {
                        "user_id": user_id, "medicine_id": medicine_id, "action": action, "quantity": quantity,
                    })
        self.assertEqual(len(self.logs()), 1)


    def test_admin_edits_details_and_receives_latest_medicine(self):
        medicine = self.add()
        result = self.db.update_medicine_details(
            medicine["id"], "  肾上腺素   注射液 ", " MRI准备室 ", "2031-02-03", self.admin,
        )
        self.assertEqual(result["name"], "肾上腺素 注射液")
        self.assertEqual(result["location"], "MRI准备室")
        self.assertEqual(result["expires_on"], "2031-02-03")
        self.assertEqual(result, self.db.get_medicine_by_id(medicine["id"], self.viewer))

    def test_staff_viewer_and_unknown_user_cannot_edit_details(self):
        medicine = self.add()
        for user in (self.staff, self.viewer, 999999):
            with self.subTest(user=user), self.assertRaises(PermissionError):
                self.db.update_medicine_details(medicine["id"], "不能修改", "新位置", "2031-01-01", user)
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer), medicine)
        self.assertEqual(len(self.logs()), 1)

    def test_edit_requires_positive_integer_id_before_opening_transaction(self):
        for medicine_id in (0, -1, True, False, "1", 1.5, None):
            with self.subTest(medicine_id=medicine_id):
                with patch.object(self.db, "connection", side_effect=AssertionError("参数错误时不应开启事务")):
                    with self.assertRaisesRegex(ValueError, "药品 ID必须是正整数"):
                        self.db.update_medicine_details(medicine_id, "药品", "柜", "2030-01-01", self.admin)

    def test_edit_rejects_empty_name_or_location_without_changes(self):
        medicine = self.add()
        for name, location in (("", "柜"), (" \t ", "柜"), (None, "柜"), (123, "柜"),
                               ("药品", ""), ("药品", " \n "), ("药品", None), ("药品", 123)):
            with self.subTest(name=name, location=location), self.assertRaises(ValueError):
                self.db.update_medicine_details(medicine["id"], name, location, "2031-01-01", self.admin)
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer), medicine)

    def test_edit_rejects_invalid_expiry_without_changes(self):
        medicine = self.add()
        for expires in (None, "", "2030-02-30", "2025-02-29", "2030-1-01", "20300101",
                        "2030-01-01T00:00:00", "0000-01-01", 20300101):
            with self.subTest(expires=expires), self.assertRaises(ValueError):
                self.db.update_medicine_details(medicine["id"], "不应生效", "新柜", expires, self.admin)
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer), medicine)

    def test_edit_nonexistent_medicine_gives_clear_error_and_does_not_insert(self):
        with self.assertRaisesRegex(LookupError, "药品不存在"):
            self.db.update_medicine_details(999999, "不能新增", "柜", "2030-01-01", self.admin)
        self.assertEqual(self.db.get_medicines(self.admin), [])
        self.assertEqual(self.logs(), [])

    def test_edit_dispensed_medicine_is_rejected_without_changes(self):
        medicine = self.add()
        self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)
        before = self.db.get_medicine_by_id(medicine["id"], self.viewer)
        logs = self.logs()
        with self.assertRaisesRegex(ValueError, "已领取药品不允许修改资料"):
            self.db.update_medicine_details(medicine["id"], "不能修改", "新柜", "2031-01-01", self.admin)
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer), before)
        self.assertEqual(self.logs(), logs)
        self.assertEqual(self.stock(), (0, 0))

    def test_edit_disabled_in_stock_medicine_preserves_disabled_state(self):
        medicine = self.add()
        self.db.disable_medicine(medicine["id"], self.admin)
        result = self.db.update_medicine_details(medicine["id"], "肾上腺素", "新柜", "2031-01-01", self.admin)
        self.assertEqual(result["is_active"], 0)
        self.assertEqual(result["status"], "in_stock")
        self.assertEqual(result["id"], medicine["id"])
        self.assertEqual(result["nfc_uuid"], medicine["nfc_uuid"])
        self.assertEqual(self.stock(), (1, 0))
        self.assertEqual(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)["reason"], "disabled")

    def test_edit_preserves_id_uuid_and_states_and_rejects_extra_fields(self):
        medicine = self.add()
        result = self.db.update_medicine_details(medicine["id"], "新名称", "新位置", "2032-01-01", self.admin)
        for key in ("id", "nfc_uuid", "status", "is_active"):
            self.assertEqual(result[key], medicine[key])
        for key, value in (("nfc_uuid", str(uuid4())), ("status", "dispensed"), ("is_active", 0)):
            with self.subTest(field=key), self.assertRaises(TypeError):
                self.db.update_medicine_details(medicine["id"], "不能修改", "柜", "2033-01-01", self.admin, **{key: value})
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer), result)

    def test_edit_expiry_recalculates_available_stock_and_keeps_book_stock(self):
        medicine = self.add(expires=(TODAY - timedelta(days=1)).isoformat())
        self.assertEqual(self.stock(), (1, 0))
        for expires, available in ((TODAY.isoformat(), 1), ("2030-01-01", 1),
                                   ((TODAY - timedelta(days=1)).isoformat(), 0)):
            result = self.db.update_medicine_details(medicine["id"], "肾上腺素", "柜", expires, self.admin)
            self.assertEqual(self.stock(), (1, available))
            self.assertEqual(result["status"], "in_stock")

    def test_edit_rolls_back_if_latest_record_cannot_be_read(self):
        medicine = self.add()
        original = self.db._execute
        class MissingRow:
            def fetchone(self):
                return None
        for mode in ("error", "missing"):
            updated = []
            def execute(connection, name, parameters=None):
                if name == "get_medicine_by_id":
                    self.assertEqual(updated, [1])  # 确认真实 UPDATE 已执行，测试的不是空事务。
                    if mode == "error":
                        raise sqlite3.OperationalError("测试回读失败")
                    return MissingRow()
                cursor = original(connection, name, parameters)
                if name == "update_medicine_details":
                    updated.append(cursor.rowcount)
                return cursor
            error_type = sqlite3.OperationalError if mode == "error" else RuntimeError
            with self.subTest(mode=mode), patch.object(self.db, "_execute", side_effect=execute):
                with self.assertRaises(error_type):
                    self.db.update_medicine_details(medicine["id"], "不应保留", "新柜", "2035-01-01", self.admin)
            self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer), medicine)
            self.assertEqual(self.stock(), (1, 1))
            self.assertEqual(len(self.logs()), 1)

    def test_edit_sql_constraint_failure_does_not_leave_partial_changes(self):
        medicine = self.add()
        original = self.db._execute
        def execute(connection, name, parameters=None):
            if name == "update_medicine_details":
                # 故障注入：让真实 SQLite UPDATE 触发日期 CHECK，验证回滚。
                parameters = dict(parameters, expires_on="2030-02-30")
            return original(connection, name, parameters)
        with patch.object(self.db, "_execute", side_effect=execute):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.update_medicine_details(medicine["id"], "不应保留", "新柜", "2035-01-01", self.admin)
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer), medicine)
        self.assertEqual(len(self.logs()), 1)

    def test_edit_requires_exactly_one_updated_row_and_rolls_back_other_counts(self):
        medicine = self.add()
        original = self.db._execute
        class UnexpectedCount:
            def __init__(self, rowcount):
                self.rowcount = rowcount
        for count in (0, -1, 2):
            def execute(connection, name, parameters=None):
                cursor = original(connection, name, parameters)
                if name == "update_medicine_details":
                    self.assertEqual(cursor.rowcount, 1)
                    return UnexpectedCount(count)
                return cursor
            with self.subTest(rowcount=count), patch.object(self.db, "_execute", side_effect=execute):
                with self.assertRaises(RuntimeError):
                    self.db.update_medicine_details(medicine["id"], "不应保留", "新柜", "2035-01-01", self.admin)
            self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.viewer), medicine)

    def test_edit_does_not_write_or_fake_operation_logs(self):
        medicine = self.add()
        fields = ("id", "user_id", "medicine_id", "action", "quantity", "created_at")
        before = [tuple(row[key] for key in fields) for row in self.logs()]
        with patch.object(self.db, "_log", side_effect=AssertionError("资料编辑不得写操作日志")):
            self.db.update_medicine_details(medicine["id"], "新名称", "新柜", "2031-01-01", self.admin)
        # 日志查询关联的是当前药名，只比较日志表自身字段，不能当作历史药名快照。
        self.assertEqual([tuple(row[key] for key in fields) for row in self.logs()], before)

    def test_edited_medicine_uses_original_uuid_and_unchanged_nfc_withdrawal(self):
        medicine = self.add()
        self.db.update_medicine_details(medicine["id"], "新药名", "新柜", TODAY.isoformat(), self.admin)
        token = self.db.find_simulated_nfc_uuid("新药名", self.staff)
        self.assertEqual(token, medicine["nfc_uuid"])
        self.assertEqual(self.db.get_medicine_by_nfc(token, self.viewer)["location"], "新柜")
        self.assertEqual(self.db.get_medicines_by_name("肾上腺素", self.viewer), [])
        result = self.db.withdraw_by_nfc(token, self.staff)
        self.assertTrue(result["success"])
        self.assertEqual(result["medicine"]["name"], "新药名")
        self.assertEqual(result["medicine"]["id"], medicine["id"])
        self.assertEqual(self.db.withdraw_by_nfc(token, self.staff)["reason"], "already_dispensed")
        self.assertEqual(self.stock("新药名"), (0, 0))
        self.assertEqual(len(self.logs(action="dispense")), 1)

    def test_edit_checks_admin_inside_immediate_transaction_and_reads_after_update(self):
        medicine = self.add()
        statements = []
        calls = []
        original_connect = self.db._connect
        original_execute = self.db._execute
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        def execute(connection, name, parameters=None):
            self.assertTrue(connection.in_transaction)
            self.assertEqual(statements[0], "BEGIN IMMEDIATE")
            calls.append(name)
            return original_execute(connection, name, parameters)
        with patch.object(self.db, "_connect", side_effect=connect), patch.object(self.db, "_execute", side_effect=execute):
            self.db.update_medicine_details(medicine["id"], "新药名", "新柜", "2031-01-01", self.admin)
        self.assertEqual(calls, ["get_user_role", "update_medicine_details", "get_medicine_by_id"])
        self.assertEqual(statements[-1], "COMMIT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
