"""CSV 导出测试：只使用标准库、独立临时数据库和临时导出目录。"""

import ast
import codecs
import csv
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch

import data_io
from medicine_db import MedicineDB


class CSVExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="medicine-csv-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database_path = self.root / "test.sqlite3"
        self.exports = self.root / "exports"
        self.exports.mkdir()
        self.output = self.exports / "medicines.csv"
        self.db = MedicineDB(self.database_path, today_provider=lambda: date(2026, 10, 22))
        self.db.init_db()
        password = "Fictional-export-test-2026"
        self.admin = self.db.create_first_admin("csv_admin", password)["id"]
        self.staff = self.db.create_user(self.admin, "csv_staff", password, "staff")["id"]
        self.viewer = self.db.create_user(self.admin, "csv_viewer", password, "viewer")["id"]

    def add(self, name="肾上腺素", expires="2030-01-01", location="虚构CT室"):
        return self.db.add_medicine(name, expires, self.admin, location)

    def export(self, path=None, user=None):
        return data_io.export_medicines_csv(
            self.db, self.admin if user is None else user, self.output if path is None else path,
        )

    def read(self, path=None):
        with (self.output if path is None else path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            return reader.fieldnames, list(reader)

    def assert_no_output_or_temporary_files(self):
        self.assertEqual(list(self.exports.iterdir()), [])

    def test_admin_exports_and_receives_path_and_count(self):
        self.add()
        result = self.export()
        self.assertEqual(result, {"path": str(self.output.resolve()), "record_count": 1})
        self.assertTrue(self.output.is_file())
        self.assertEqual(list(self.exports.iterdir()), [self.output])

    def test_staff_is_rejected_before_query_or_file_creation(self):
        with patch.object(self.db, "get_medicines", side_effect=AssertionError("越权不能查询")):
            with self.assertRaises(PermissionError):
                self.export(user=self.staff)
        self.assert_no_output_or_temporary_files()

    def test_viewer_is_rejected_before_query_or_file_creation(self):
        with patch.object(self.db, "get_medicines", side_effect=AssertionError("越权不能查询")):
            with self.assertRaises(PermissionError):
                self.export(user=self.viewer)
        self.assert_no_output_or_temporary_files()

    def test_unknown_user_and_invalid_user_ids_are_rejected(self):
        with self.assertRaises(PermissionError):
            self.export(user=999999)
        for value in (True, 0, -1, "1", None):
            with self.subTest(user_id=value), self.assertRaises(ValueError):
                data_io.export_medicines_csv(self.db, value, self.output)
        self.assert_no_output_or_temporary_files()

    def test_fixed_fields_header_and_values(self):
        medicine = self.add()
        self.export()
        fields, rows = self.read()
        self.assertEqual(fields, ["id", "nfc_uuid", "name", "location", "expires_on", "status", "is_active"])
        self.assertEqual(rows, [{key: str(medicine[key]) for key in fields}])
        self.assertNotIn("password", fields)
        self.assertNotIn("username", fields)

    def test_chinese_text_and_utf8_bom(self):
        self.add(name="盐酸肾上腺素注射液", location="虚构影像科急救柜")
        self.export()
        contents = self.output.read_bytes()
        self.assertTrue(contents.startswith(codecs.BOM_UTF8))
        self.assertIn("盐酸肾上腺素注射液", contents.decode("utf-8-sig"))
        self.assertEqual(self.read()[1][0]["location"], "虚构影像科急救柜")

    def test_all_statuses_including_dispensed_disabled_and_expired_are_exported(self):
        in_stock = self.add()
        dispensed = self.add(name="虚构药品B")
        disabled = self.add(name="虚构药品C")
        expired = self.add(name="虚构药品D", expires="2020-01-01")
        self.db.withdraw_by_nfc(dispensed["nfc_uuid"], self.staff)
        self.db.disable_medicine(disabled["id"], self.admin)
        self.assertEqual(self.export()["record_count"], 4)
        rows = {int(row["id"]): row for row in self.read()[1]}
        self.assertEqual(rows[in_stock["id"]]["status"], "in_stock")
        self.assertEqual(rows[dispensed["id"]]["status"], "dispensed")
        self.assertEqual(rows[disabled["id"]]["is_active"], "0")
        self.assertEqual(rows[expired["id"]]["expires_on"], "2020-01-01")

    def test_empty_medicine_table_still_exports_header(self):
        self.assertEqual(self.export()["record_count"], 0)
        fields, rows = self.read()
        self.assertEqual(fields, list(data_io.CSV_FIELDS))
        self.assertEqual(rows, [])
        self.assertTrue(self.output.read_bytes().startswith(codecs.BOM_UTF8))

    def test_existing_file_is_not_overwritten(self):
        contents = b"user-owned-existing-content"
        self.output.write_bytes(contents)
        with self.assertRaisesRegex(FileExistsError, "拒绝覆盖"):
            self.export()
        self.assertEqual(self.output.read_bytes(), contents)
        self.assertEqual(list(self.exports.iterdir()), [self.output])

    def test_existing_symlink_or_dangling_symlink_is_not_followed(self):
        destination = self.root / "protected.txt"
        destination.write_text("保留原文件", encoding="utf-8")
        for target in (destination, self.root / "missing.txt"):
            self.output.symlink_to(target)
            with self.subTest(target=target), self.assertRaises(FileExistsError):
                self.export()
            self.assertTrue(self.output.is_symlink())
            self.output.unlink()  # 只删除本测试创建的临时链接。
        self.assertEqual(destination.read_text(encoding="utf-8"), "保留原文件")
        self.assertFalse((self.root / "missing.txt").exists())

    def test_existing_database_path_is_not_overwritten(self):
        before = self.database_path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.export(path=self.database_path)
        self.assertEqual(self.database_path.read_bytes(), before)

    def test_missing_directory_gives_clear_error_and_is_not_created(self):
        missing = self.root / "does-not-exist"
        with self.assertRaisesRegex(FileNotFoundError, "目标目录不存在"):
            self.export(path=missing / "medicines.csv")
        self.assertFalse(missing.exists())

    def test_parent_file_is_not_treated_as_directory(self):
        parent = self.root / "ordinary-file"
        parent.write_text("保留", encoding="utf-8")
        with self.assertRaisesRegex(NotADirectoryError, "不是目录"):
            self.export(path=parent / "medicines.csv")
        self.assertEqual(parent.read_text(encoding="utf-8"), "保留")

    def test_empty_invalid_and_directory_paths_are_rejected(self):
        for value in ("", " \t ", None, 123, b"file.csv", "bad\x00.csv", str(self.exports) + os.sep):
            with self.subTest(path=value), self.assertRaises(ValueError):
                data_io.export_medicines_csv(self.db, self.admin, value)
        with self.assertRaises(IsADirectoryError):
            self.export(path=self.exports)
        self.assert_no_output_or_temporary_files()

    def test_permission_failure_is_reported_without_output(self):
        # 故障注入模拟操作系统拒绝写入，避免测试依赖当前账户是否有超级用户权限。
        with patch.object(data_io.tempfile, "NamedTemporaryFile", side_effect=PermissionError("测试禁止写入")):
            with self.assertRaisesRegex(PermissionError, "没有权限写入 CSV"):
                self.export()
        self.assert_no_output_or_temporary_files()

    def test_database_query_failure_leaves_no_csv_or_temporary_file(self):
        with patch.object(self.db, "get_medicines", side_effect=sqlite3.OperationalError("测试查询失败")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "测试查询失败"):
                self.export()
        self.assert_no_output_or_temporary_files()

    def test_partial_write_failure_removes_temporary_file_and_never_publishes_target(self):
        self.add()
        original_writer = csv.DictWriter
        class BrokenWriter:
            def __init__(self, *args, **kwargs):
                self.writer = original_writer(*args, **kwargs)
            def writeheader(self):
                self.writer.writeheader()
            def writerow(self, row):
                self.writer.writerow(row)
                raise OSError("测试写入中途失败")
        before = self.database_path.read_bytes()
        with patch.object(data_io.csv, "DictWriter", BrokenWriter):
            with self.assertRaisesRegex(OSError, "CSV 文件保存失败"):
                self.export()
        self.assert_no_output_or_temporary_files()
        self.assertEqual(self.database_path.read_bytes(), before)

    def test_flush_to_disk_failure_never_publishes_target(self):
        self.add()
        with patch.object(data_io.os, "fsync", side_effect=OSError("测试磁盘写入失败")):
            with self.assertRaisesRegex(OSError, "CSV 文件保存失败"):
                self.export()
        self.assert_no_output_or_temporary_files()

    def test_publish_failure_removes_temporary_file(self):
        with patch.object(data_io.os, "link", side_effect=OSError("测试文件系统不支持硬链接")):
            with self.assertRaisesRegex(OSError, "CSV 文件保存失败"):
                self.export()
        self.assert_no_output_or_temporary_files()

    def test_file_created_after_initial_check_is_not_overwritten_or_deleted(self):
        self.add()
        original_link = os.link
        winner = b"another-request-created-this-file"
        def raced_link(source, target):
            target.write_bytes(winner)
            return original_link(source, target)
        with patch.object(data_io.os, "link", side_effect=raced_link):
            with self.assertRaisesRegex(FileExistsError, "拒绝覆盖"):
                self.export()
        self.assertEqual(self.output.read_bytes(), winner)
        self.assertEqual(list(self.exports.iterdir()), [self.output])

    def test_export_preserves_entire_database_bytes_and_inventory(self):
        first = self.add()
        second = self.add(name="虚构药品B")
        self.db.disable_medicine(second["id"], self.admin)
        self.db.withdraw_by_nfc(first["nfc_uuid"], self.staff)
        before = self.database_path.read_bytes()
        inventory = self.db.get_stock_by_name(self.admin)
        self.export()
        self.assertEqual(self.database_path.read_bytes(), before)
        self.assertEqual(self.db.get_stock_by_name(self.admin), inventory)

    def test_export_does_not_add_or_change_operation_logs(self):
        self.add()
        before = self.db.get_operation_logs(self.admin)
        self.export()
        self.assertEqual(self.db.get_operation_logs(self.admin), before)

    def test_original_nfc_still_dispenses_once_after_export(self):
        medicine = self.add()
        self.export()
        self.assertEqual(self.db.get_medicine_by_nfc(medicine["nfc_uuid"], self.viewer), medicine)
        self.assertTrue(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)["success"])
        self.assertEqual(self.db.withdraw_by_nfc(medicine["nfc_uuid"], self.staff)["reason"], "already_dispensed")
        after = self.exports / "after.csv"
        self.export(path=after)
        self.assertEqual(self.read(after)[1][0]["status"], "dispensed")
        self.assertEqual(len(self.db.get_operation_logs(self.admin, action="dispense")), 1)

    def test_formula_text_is_escaped_only_in_export_and_database_values_are_original(self):
        formulas = ("=1+1", "+SUM(1,2)", "-1+2", "@SUM(1,2)")
        for text in formulas:
            self.add(name=text, location=text)
        before = self.database_path.read_bytes()
        self.export()
        rows = self.read()[1]
        self.assertEqual([row["name"] for row in rows], ["'" + text for text in formulas])
        self.assertEqual([row["location"] for row in rows], ["'" + text for text in formulas])
        self.assertEqual([row["name"] for row in self.db.get_medicines(self.admin)], list(formulas))
        self.assertEqual(self.database_path.read_bytes(), before)

    def test_formula_guard_handles_leading_whitespace_controls_and_bom(self):
        samples = (" =1", "\t=1", "\r=1", "\n=1", "\x00=1", "\ufeff=1", "\u3000+1", "\t普通文本")
        for value in samples:
            with self.subTest(value=value):
                self.assertEqual(data_io._safe_csv_value(value), "'" + value)
        for value in ("正常中文", "2030-01-01", "in_stock", "", "药品=1", 1, 0):
            with self.subTest(value=value):
                self.assertEqual(data_io._safe_csv_value(value), value)

    def test_commas_quotes_and_embedded_newlines_are_preserved_as_single_cells(self):
        medicine = self.add(name='虚构药品,"演示"', location='虚构CT室,"A区"\n第二层')
        self.export()
        _, rows = self.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], medicine["name"])
        self.assertEqual(rows[0]["location"], medicine["location"])

    def test_public_permission_and_medicine_query_methods_are_used(self):
        self.add()
        with patch.object(self.db, "check_permission", wraps=self.db.check_permission) as permission:
            with patch.object(self.db, "get_medicines", wraps=self.db.get_medicines) as query:
                self.export()
        permission.assert_called_once_with(self.admin, ("admin",))
        query.assert_called_once_with(self.admin)
        tree = ast.parse(Path(data_io.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertNotIn("sqlite3", [item.name for item in node.names])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, ("execute", "executescript", "connect"))

    def test_two_concurrent_exports_to_same_target_publish_one_complete_file(self):
        self.add()
        gate = threading.Barrier(2)
        def export(_):
            gate.wait(timeout=10)
            try:
                return self.export()
            except FileExistsError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(export, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.read()[1]), 1)
        self.assertEqual(list(self.exports.iterdir()), [self.output])

    def test_relative_path_returns_normalized_absolute_path(self):
        relative = os.path.relpath(self.output, Path.cwd())
        self.assertEqual(self.export(path=relative)["path"], str(self.output.resolve()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
