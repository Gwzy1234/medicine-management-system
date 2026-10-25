"""接口测试全部使用临时库；不读取环境变量中的正式数据库路径。"""

import ast
import asyncio
import codecs
import csv
import hashlib
import io
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import jwt
from fastapi.testclient import TestClient

from medicine_db import DatabaseInitializationError, MedicineDB
from backend import api
from backend.auth import AUDIENCE, ISSUER, create_access_token
from backend.config import Settings, load_settings
from backend.main import create_app


PASSWORD = "Fictional-API-test-password-2026"
TODAY = date(2026, 10, 22)


class APITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="medicine-api-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = MedicineDB(self.root / "test.sqlite3", today_provider=lambda: TODAY)
        self.db.init_db()
        admin = self.db.create_first_admin("admin", PASSWORD)
        self.users = {"admin": admin}
        for role in ("staff", "viewer"):
            self.users[role] = self.db.create_user(admin["id"], role, PASSWORD, role)
        self.settings = Settings(self.db.path, secrets.token_urlsafe(48), sqlite_timeout=0.02)
        self.app = create_app(self.settings, self.db)
        self.client = self.enterContext(TestClient(self.app, raise_server_exceptions=False))
        self.headers = {role: {"Authorization": "Bearer " + create_access_token(user["id"], self.settings)["access_token"]}
                        for role, user in self.users.items()}

    def request(self, method, path, *, role="admin", **kwargs):
        return self.client.request(method, path, headers=self.headers[role], **kwargs)

    def register(self, name="肾上腺素", expires="2030-01-01", location="虚构CT室"):
        response = self.request("POST", "/api/medicines", json={"name": name, "expires_on": expires, "location": location})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["data"]

    def dispense(self, medicine, role="staff"):
        return self.request("POST", "/api/nfc/dispense", role=role, json={key: medicine[key] for key in ("nfc_uuid", "name", "location", "expires_on")})

    def token(self, **changes):
        now = int(time.time())
        claims = {"sub": str(self.users["staff"]["id"]), "iat": now, "exp": now + 60,
                  "iss": ISSUER, "aud": AUDIENCE}
        claims.update(changes)
        return jwt.encode(claims, self.settings.jwt_secret, algorithm="HS256")

    def use_token(self, token):
        return self.client.get("/api/auth/me", headers={"Authorization": "Bearer " + token})

    def test_login_returns_expiring_token_and_public_user(self):
        with patch.object(self.db, "login_user", wraps=self.db.login_user) as login:
            response = self.client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        login.assert_called_once_with("admin", PASSWORD)
        result = response.json()["data"]
        self.assertEqual(result["token_type"], "bearer")
        self.assertEqual(result["expires_in"], 1800)
        self.assertEqual(set(result["user"]), {"id", "username", "role"})
        self.assertEqual(self.use_token(result["access_token"]).status_code, 200)

    def test_wrong_password_and_unknown_account_are_generic_401(self):
        errors = []
        for username, password in (("admin", "incorrect-password"), ("missing", PASSWORD)):
            response = self.client.post("/api/auth/login", json={"username": username, "password": password})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
            errors.append(response.json())
            self.assertNotIn(password, response.text)
        self.assertEqual(errors[0], errors[1])

    def test_all_business_routes_require_authentication(self):
        endpoints = [("GET", "/api/auth/me"), ("GET", "/api/users"), ("GET", "/api/users/1"),
                     ("GET", "/api/medicines"), ("GET", "/api/medicines/1"),
                     ("GET", "/api/inventory"), ("GET", "/api/inventory/by-name?name=药品"),
                     ("GET", "/api/logs"), ("GET", "/api/export/medicines"),
                     ("POST", "/api/nfc/scan"), ("POST", "/api/nfc/simulate"),
                     ("POST", "/api/nfc/dispense"), ("POST", "/api/medicines"),
                     ("PATCH", "/api/medicines/1"), ("POST", "/api/users"),
                     ("POST", "/api/medicines/1/enable"), ("POST", "/api/medicines/1/disable")]
        for method, path in endpoints:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertIn("error", response.json())

    def test_expired_token_is_rejected(self):
        self.assertEqual(self.use_token(self.token(iat=int(time.time()) - 120, exp=int(time.time()) - 1)).status_code, 401)

    def test_wrong_signature_tampered_and_unsigned_tokens_are_rejected(self):
        good = self.token()
        claims = jwt.decode(good, options={"verify_signature": False})
        wrong = jwt.encode(claims, secrets.token_urlsafe(48), algorithm="HS256")
        unsigned = jwt.encode(claims, "", algorithm="none")
        for token in (wrong, unsigned, good.rsplit(".", 1)[0] + ".invalid-signature"):
            with self.subTest(case=token == unsigned):
                self.assertEqual(self.use_token(token).status_code, 401)

    def test_malformed_token_claims_and_structure_are_rejected(self):
        tokens = ["not-a-jwt", "a.b.c", "x" * 8193]
        for changes in ({"sub": "0"}, {"sub": "01"}, {"sub": "١"}, {"sub": 1},
                        {"sub": "9999999999999999999"}, {"exp": None}, {"exp": []},
                        {"exp": float("inf")}, {"exp": True}, {"iat": "bad"},
                        {"iat": int(time.time()) + 600}, {"iss": "wrong"}, {"aud": "wrong"}):
            tokens.append(self.token(**changes))
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        for field in ("sub", "exp", "iat", "iss", "aud"):
            tokens.append(jwt.encode({key: value for key, value in claims.items() if key != field},
                                     self.settings.jwt_secret, algorithm="HS256"))
        for index, token in enumerate(tokens):
            with self.subTest(index=index):
                self.assertEqual(self.use_token(token).status_code, 401)

    def test_unknown_token_user_and_wrong_auth_scheme_are_rejected(self):
        self.assertEqual(self.use_token(self.token(sub="999999")).status_code, 401)
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": "Basic abc"}).status_code, 401)

    def test_token_role_claim_is_not_used_and_database_role_is_current(self):
        token = self.token(role="admin")
        response = self.client.get("/api/users", headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 403)
        with self.db.connection(write=True) as connection:
            # 仅改变临时库的测试夹具，验证请求不会信任旧角色。
            connection.execute("UPDATE users SET role=? WHERE id=?", ("viewer", self.users["admin"]["id"]))
        self.assertEqual(self.request("GET", "/api/users").status_code, 403)
        self.assertEqual(self.request("GET", "/api/auth/me").json()["data"]["role"], "viewer")

    def test_body_and_query_cannot_override_actor_or_business_state(self):
        medicine = self.register()
        bodies = [("/api/nfc/dispense", {"nfc_uuid": medicine["nfc_uuid"], "user_id": self.users["admin"]["id"]}),
                  ("/api/nfc/scan", {"nfc_uuid": medicine["nfc_uuid"], "role": "admin"}),
                  ("/api/nfc/simulate", {"name": "肾上腺素", "today": "2000-01-01"}),
                  ("/api/medicines", {"name": "药品", "expires_on": "2030-01-01", "nfc_uuid": str(uuid4())})]
        for path, body in bodies:
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, json=body).status_code, 422)
        for extra in ("id", "nfc_uuid", "status", "is_active", "user_id", "role"):
            body = {"name": "药品", "location": "柜", "expires_on": "2030-01-01", extra: "forged"}
            self.assertEqual(self.request("PATCH", f'/api/medicines/{medicine["id"]}', json=body).status_code, 422)
        self.assertEqual(self.request("GET", "/api/export/medicines?output_path=/tmp/forged.csv").status_code, 422)
        self.assertEqual(self.request("POST", "/api/nfc/dispense?user_id=1", json={"nfc_uuid": medicine["nfc_uuid"]}).status_code, 422)
        self.assertEqual(self.db.get_medicine_by_id(medicine["id"], self.users["viewer"]["id"]), medicine)

    def test_staff_and_viewer_can_read_medicines_inventory_and_scan(self):
        medicine = self.register()
        for role in ("staff", "viewer"):
            for path in ("/api/medicines", f'/api/medicines/{medicine["id"]}', "/api/inventory",
                         "/api/inventory/by-name?name=肾上腺素", "/api/auth/me"):
                self.assertEqual(self.request("GET", path, role=role).status_code, 200)
            self.assertEqual(self.request("POST", "/api/nfc/scan", role=role, json={"nfc_uuid": medicine["nfc_uuid"]}).status_code, 200)
            self.assertEqual(self.request("POST", "/api/nfc/simulate", role=role, json={"name": "肾上腺素"}).status_code, 200)

    def test_staff_and_viewer_cannot_manage_or_export(self):
        medicine = self.register()
        for role in ("staff", "viewer"):
            self.assertEqual(self.request("GET", "/api/users", role=role).status_code, 403)
            self.assertEqual(self.request("GET", "/api/users/1", role=role).status_code, 403)
            self.assertEqual(self.request("POST", "/api/users", role=role,
                                          json={"username": "forged", "password": PASSWORD, "role": "admin"}).status_code, 403)
            self.assertEqual(self.request("POST", "/api/medicines", role=role,
                                          json={"name": "药品", "expires_on": "2030-01-01"}).status_code, 403)
            self.assertEqual(self.request("PATCH", f'/api/medicines/{medicine["id"]}', role=role,
                                          json={"name": "药品", "location": "柜", "expires_on": "2030-01-01"}).status_code, 403)
            for operation in ("enable", "disable"):
                self.assertEqual(self.request("POST", f'/api/medicines/{medicine["id"]}/{operation}', role=role).status_code, 403)
            self.assertEqual(self.request("GET", "/api/export/medicines", role=role).status_code, 403)

    def test_admin_user_management_and_duplicate_username(self):
        response = self.request("POST", "/api/users", json={"username": "new_viewer", "password": PASSWORD, "role": "viewer"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.request("GET", f'/api/users/{response.json()["data"]["id"]}').json(), response.json())
        self.assertEqual(len(self.request("GET", "/api/users").json()["data"]), 4)
        self.assertEqual(self.request("POST", "/api/users", json={"username": "new_viewer", "password": PASSWORD}).status_code, 409)
        self.assertEqual(self.request("GET", "/api/users/999999").status_code, 404)

    def test_registration_uses_existing_method_and_uuid_generation(self):
        with patch.object(self.db, "add_medicine", wraps=self.db.add_medicine) as register:
            medicine = self.register()
        register.assert_called_once_with(name="肾上腺素", expires_on="2030-01-01", user_id=self.users["admin"]["id"], location="虚构CT室")
        self.assertEqual(UUID(medicine["nfc_uuid"]).version, 4)
        self.assertEqual(medicine["status"], "in_stock")

    def test_duplicate_generated_uuid_conflict_does_not_add_medicine_or_log(self):
        medicine = self.register()
        with patch("medicine_db.uuid4", return_value=UUID(medicine["nfc_uuid"])):
            response = self.request("POST", "/api/medicines", json={"name": "重复", "expires_on": "2030-01-01"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.db.get_medicines(self.users["admin"]["id"])), 1)
        self.assertEqual(len(self.db.get_operation_logs(self.users["admin"]["id"])), 1)

    def test_edit_calls_existing_method_and_preserves_protected_fields(self):
        medicine = self.register()
        with patch.object(self.db, "update_medicine_details", wraps=self.db.update_medicine_details) as edit:
            response = self.request("PATCH", f'/api/medicines/{medicine["id"]}',
                                    json={"name": "新药名", "location": "新柜", "expires_on": "2031-01-01"})
        self.assertEqual(response.status_code, 200)
        edit.assert_called_once_with(medicine_id=medicine["id"], name="新药名", location="新柜",
                                     expires_on="2031-01-01", user_id=self.users["admin"]["id"])
        for field in ("id", "nfc_uuid", "status", "is_active"):
            self.assertEqual(response.json()["data"][field], medicine[field])

    def test_patch_requires_all_three_editable_fields(self):
        medicine = self.register()
        self.assertEqual(self.request("PATCH", f'/api/medicines/{medicine["id"]}', json={"location": "新柜"}).status_code, 422)

    def test_dispensed_medicine_edit_is_409(self):
        medicine = self.register()
        self.assertEqual(self.dispense(medicine).status_code, 200)
        response = self.request("PATCH", f'/api/medicines/{medicine["id"]}',
                                json={"name": "新药名", "location": "柜", "expires_on": "2031-01-01"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "already_dispensed")

    def test_invalid_dates_and_types_are_422_and_service_stays_usable(self):
        for value in ("2030-02-30", "20300101", "2030-1-01", None, 20300101):
            self.assertEqual(self.request("POST", "/api/medicines", json={"name": "药品", "expires_on": value}).status_code, 422)
        for path in ("/api/medicines/0", "/api/medicines/not-an-id", "/api/medicines?category=unknown", "/api/medicines?days=-1"):
            self.assertEqual(self.request("GET", path).status_code, 422)
        self.assertEqual(self.request("GET", "/api/inventory").status_code, 200)

    def test_enable_disable_and_repeated_disable_are_idempotent(self):
        medicine = self.register()
        path = f'/api/medicines/{medicine["id"]}'
        self.assertTrue(self.request("POST", path + "/disable").json()["data"]["changed"])
        self.assertFalse(self.request("POST", path + "/disable").json()["data"]["changed"])
        self.assertTrue(self.request("POST", path + "/enable").json()["data"]["changed"])
        self.assertEqual(self.request("GET", path).json()["data"]["status"], "in_stock")
        self.assertEqual(len(self.db.get_operation_logs(self.users["admin"]["id"], action="disable")), 1)

    def test_nonexistent_medicines_are_404_for_each_operation(self):
        for operation in ("enable", "disable"):
            self.assertEqual(self.request("POST", "/api/medicines/999999/" + operation).status_code, 404)
        self.assertEqual(self.request("GET", "/api/medicines/999999").status_code, 404)
        self.assertEqual(self.request("PATCH", "/api/medicines/999999", json={"name": "药品", "location": "柜", "expires_on": "2030-01-01"}).status_code, 404)

    def test_medicine_name_and_state_filters_intersect(self):
        active = self.register()
        self.register(name="另一药名")
        disabled = self.register()
        self.request("POST", f'/api/medicines/{disabled["id"]}/disable')
        response = self.request("GET", "/api/medicines", params={"name": "肾上腺素", "category": "disabled"})
        self.assertEqual([row["id"] for row in response.json()["data"]], [disabled["id"]])
        self.assertEqual(len(self.request("GET", "/api/medicines", params={"name": "肾上腺素"}).json()["data"]), 2)
        self.assertEqual(self.request("GET", f'/api/medicines/{active["id"]}').status_code, 200)

    def test_scan_and_simulate_are_readonly_and_use_existing_uuid(self):
        medicine = self.register()
        before = self.db.path.read_bytes()
        with patch("medicine_db.uuid4", side_effect=AssertionError("扫描不得生成 UUID")):
            scanned = self.request("POST", "/api/nfc/scan", json={"nfc_uuid": medicine["nfc_uuid"]})
            simulated = self.request("POST", "/api/nfc/simulate", json={"name": "肾上腺素"})
        self.assertEqual(scanned.json(), simulated.json())
        self.assertEqual(scanned.json()["data"], {**medicine, "business_date": TODAY.isoformat(), "can_dispense": True})
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_simulation_skips_expired_disabled_and_dispensed_and_never_dispenses(self):
        self.register(expires="2020-01-01")
        disabled = self.register()
        self.db.disable_medicine(disabled["id"], self.users["admin"]["id"])
        dispensed = self.register()
        self.dispense(dispensed)
        eligible = self.register()
        response = self.request("POST", "/api/nfc/simulate", role="viewer", json={"name": "肾上腺素"})
        self.assertEqual(response.json()["data"]["nfc_uuid"], eligible["nfc_uuid"])
        self.assertEqual(response.json()["data"]["status"], "in_stock")
        self.assertEqual(self.request("POST", "/api/nfc/simulate", json={"name": "不存在"}).status_code, 404)

    def test_both_scans_use_one_dispense_method_and_repeat_is_409(self):
        medicine = self.register()
        scanned = self.request("POST", "/api/nfc/scan", json={"nfc_uuid": medicine["nfc_uuid"]}).json()["data"]
        simulated = self.request("POST", "/api/nfc/simulate", json={"name": "肾上腺素"}).json()["data"]
        with patch.object(self.db, "withdraw_by_nfc", wraps=self.db.withdraw_by_nfc) as withdraw:
            first = self.dispense(scanned)
            repeated = self.dispense(simulated)
        self.assertEqual([first.status_code, repeated.status_code], [200, 409])
        self.assertEqual(withdraw.call_count, 2)
        withdraw.assert_called_with(medicine["nfc_uuid"], self.users["staff"]["id"],
                                    confirmed_details={key: medicine[key] for key in ("name", "location", "expires_on")})
        self.assertEqual(len(self.db.get_operation_logs(self.users["admin"]["id"], action="dispense")), 1)

    def test_concurrent_http_dispense_succeeds_once(self):
        medicine = self.register()
        # 给并发请求足够的锁等待时间，避免把忙错误误当作重复领取结果。
        self.db.timeout = 5
        gate = threading.Barrier(2)
        def dispense(_):
            gate.wait(timeout=10)
            return self.dispense(medicine).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(dispense, range(2)))
        self.assertEqual(sorted(codes), [200, 409])
        self.assertEqual(len(self.db.get_operation_logs(self.users["admin"]["id"], action="dispense")), 1)

    def test_expired_disabled_viewer_and_unknown_uuid_cannot_dispense(self):
        expired = self.register(expires="2020-01-01")
        disabled = self.register()
        self.db.disable_medicine(disabled["id"], self.users["admin"]["id"])
        self.assertEqual(self.dispense(expired).json()["error"]["code"], "expired")
        self.assertEqual(self.dispense(disabled).json()["error"]["code"], "disabled")
        self.assertEqual(self.dispense(disabled, role="viewer").status_code, 403)
        before = self.db.path.read_bytes()
        for path in ("/api/nfc/scan", "/api/nfc/dispense"):
            body = {"nfc_uuid": str(uuid4())}
            if path.endswith("dispense"):
                body.update(name="未知药品", location="未知柜", expires_on="2030-01-01")
            self.assertEqual(self.request("POST", path, json=body).status_code, 404)
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_get_and_readonly_scan_never_dispense(self):
        medicine = self.register()
        before = self.db.path.read_bytes()
        for path in ("/api/medicines", f'/api/medicines/{medicine["id"]}', "/api/inventory", "/api/nfc/dispense"):
            self.request("GET", path)
        self.request("POST", "/api/nfc/scan", json={"nfc_uuid": medicine["nfc_uuid"]})
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_inventory_updates_after_edit_and_dispense_and_today_is_valid(self):
        medicine = self.register(expires="2020-01-01")
        def stock():
            return self.request("GET", "/api/inventory/by-name", params={"name": "肾上腺素"}).json()["data"]
        self.assertEqual((stock()["stock"], stock()["available_stock"]), (1, 0))
        edited = self.request("PATCH", f'/api/medicines/{medicine["id"]}', json={"name": "肾上腺素", "location": "柜", "expires_on": TODAY.isoformat()})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual((stock()["stock"], stock()["available_stock"]), (1, 1))
        self.assertEqual(self.dispense(edited.json()["data"]).status_code, 200)
        self.assertEqual((stock()["stock"], stock()["available_stock"]), (0, 0))

    def test_logs_are_admin_all_staff_own_viewer_forbidden_and_filters_combine(self):
        medicine = self.register()
        self.dispense(medicine)
        self.assertEqual(len(self.request("GET", "/api/logs").json()["data"]), 2)
        staff = self.request("GET", "/api/logs", role="staff").json()["data"]
        self.assertEqual([row["action"] for row in staff], ["dispense"])
        self.assertEqual(self.request("GET", "/api/logs", role="viewer").status_code, 403)
        self.assertEqual(self.request("GET", "/api/logs", role="staff", params={"user_id": self.users["admin"]["id"]}).status_code, 403)
        filtered = self.request("GET", "/api/logs", params={"user_id": self.users["staff"]["id"], "medicine_id": medicine["id"], "action": "dispense"})
        self.assertEqual(filtered.json()["data"], staff)

    def test_csv_uses_existing_export_and_cleans_temporary_directory_after_download(self):
        self.register()
        before = self.db.path.read_bytes()
        created = []
        original = tempfile.TemporaryDirectory
        def directory(*args, **kwargs):
            temp = original(prefix="download-", dir=self.root)
            created.append(Path(temp.name))
            return temp
        with patch.object(api.tempfile, "TemporaryDirectory", side_effect=directory):
            with patch.object(api.data_io, "export_medicines_csv", wraps=api.data_io.export_medicines_csv) as export:
                response = self.request("GET", "/api/export/medicines")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(response.content.startswith(codecs.BOM_UTF8))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))), 1)
        self.assertEqual(export.call_count, 1)
        self.assertTrue(created and all(not path.exists() for path in created))
        self.assertEqual(self.db.path.read_bytes(), before)
        self.assertNotIn(str(self.root), response.headers["content-disposition"])

    def test_csv_failure_cleans_directory_and_does_not_leak_paths(self):
        created = []
        original = tempfile.TemporaryDirectory
        def directory(*args, **kwargs):
            temp = original(prefix="failed-download-", dir=self.root)
            created.append(Path(temp.name))
            return temp
        with patch.object(api.tempfile, "TemporaryDirectory", side_effect=directory):
            with patch.object(api.data_io, "export_medicines_csv", side_effect=OSError(str(self.root))):
                response = self.request("GET", "/api/export/medicines")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(str(self.root), response.text)
        self.assertTrue(created and all(not path.exists() for path in created))

    def test_csv_range_request_returns_uniform_json_error_without_creating_file(self):
        headers = dict(self.headers["admin"], Range="bytes=broken")
        with patch.object(api.tempfile, "TemporaryDirectory", side_effect=AssertionError("不应创建导出文件")):
            response = self.client.get("/api/export/medicines", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_range")

    def test_csv_cleanup_also_runs_when_sending_fails(self):
        temporary = tempfile.TemporaryDirectory(prefix="send-failure-", dir=self.root)
        path = Path(temporary.name) / "data.csv"
        path.write_text("id\n1\n", encoding="utf-8")
        response = api.TemporaryCSVResponse(path, temporary)
        async def send(message):
            raise OSError("测试客户端断开")
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        with self.assertRaises(OSError):
            asyncio.run(response({"type": "http", "method": "GET", "headers": [], "extensions": {},
                                  "asgi": {"spec_version": "2.4"}}, receive, send))
        self.assertFalse(path.parent.exists())

    def test_log_failure_rolls_back_register_dispense_disable_and_enable(self):
        medicine = self.register()
        original = self.db._execute
        def broken(connection, name, parameters=None):
            if name == "add_operation_log":
                parameters = dict(parameters, quantity=0)
            return original(connection, name, parameters)
        cases = [("POST", "/api/medicines", {"name": "失败登记", "expires_on": "2030-01-01"}),
                 ("POST", "/api/nfc/dispense", {key: medicine[key] for key in ("nfc_uuid", "name", "location", "expires_on")}),
                 ("POST", f'/api/medicines/{medicine["id"]}/disable', None)]
        for method, path, body in cases:
            before = self.db.path.read_bytes()
            with patch.object(self.db, "_execute", side_effect=broken):
                response = self.request(method, path, json=body)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(self.db.path.read_bytes(), before)
        self.db.disable_medicine(medicine["id"], self.users["admin"]["id"])
        before = self.db.path.read_bytes()
        with patch.object(self.db, "_execute", side_effect=broken):
            self.assertEqual(self.request("POST", f'/api/medicines/{medicine["id"]}/enable').status_code, 500)
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_database_lock_timeout_returns_503_without_success_log(self):
        medicine = self.register()
        self.db.timeout = 0.02
        with self.db.connection(write=True):
            response = self.dispense(medicine)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertEqual(self.db.get_operation_logs(self.users["admin"]["id"], action="dispense"), [])

    def test_database_and_unexpected_errors_are_not_success_or_sensitive_responses(self):
        for error in (sqlite3.OperationalError("SELECT secret FROM " + str(self.root)), RuntimeError(PASSWORD)):
            with patch.object(self.db, "withdraw_by_nfc", side_effect=error):
                response = self.request("POST", "/api/nfc/dispense", json={"nfc_uuid": str(uuid4()), "name": "虚构药品", "location": "虚构柜", "expires_on": "2030-01-01"})
            self.assertEqual(response.status_code, 500)
            for secret in ("SELECT", PASSWORD, str(self.root), "Traceback"):
                self.assertNotIn(secret, response.text)
            self.assertNotIn("data", response.json())
        self.assertEqual(self.request("GET", "/api/inventory").status_code, 200)

    def test_validation_errors_do_not_echo_password_or_token_and_logs_do_not_record_them(self):
        secret = "never-echo-this-password"
        with self.assertNoLogs("backend", logging.WARNING):
            response = self.client.post("/api/auth/login", json={"username": "admin", "password": secret, "extra": secret})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("input", response.text)

    def test_response_models_filter_sensitive_extra_fields(self):
        record = dict(self.users["admin"], password="must-not-leak", password_hash="must-not-leak")
        with patch.object(self.db, "get_users", return_value=[record]):
            response = self.request("GET", "/api/users")
        self.assertEqual(set(response.json()["data"][0]), {"id", "username", "role"})
        self.assertNotIn("must-not-leak", response.text)

    def test_docs_routes_cors_policy_and_error_structure(self):
        for path in ("/docs", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/missing").json()["error"]["code"], "http_error")
        response = self.client.options("/api/medicines", headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", response.headers)
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/api/nfc/scan", schema["paths"])
        self.assertNotIn("/api/medicines/nfc/{nfc_uuid}", schema["paths"])
        self.assertIn("ErrorResponse", schema["components"]["schemas"])


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="medicine-startup-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "test.sqlite3"
        self.secret = secrets.token_urlsafe(48)

    def test_missing_or_invalid_secret_and_bad_config_reject_app_creation(self):
        for secret in ("", "short", " " * 64):
            with self.assertRaises(ValueError):
                Settings(self.path, secret)
        for minutes in (0, -1, True, "30", 1441):
            with self.assertRaises(ValueError):
                Settings(self.path, self.secret, token_minutes=minutes)
        for timeout in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                Settings(self.path, self.secret, sqlite_timeout=timeout)
        with patch.dict(os.environ, {"MEDICINE_DB_PATH": str(self.path), "MEDICINE_JWT_SECRET": ""}, clear=True):
            with self.assertRaises(ValueError):
                create_app()
        self.assertFalse(self.path.exists())

    def test_environment_settings_and_secret_repr(self):
        env = {"MEDICINE_DB_PATH": str(self.path), "MEDICINE_JWT_SECRET": self.secret,
               "TOKEN_EXPIRE_MINUTES": "15", "SQLITE_TIMEOUT_SECONDS": "0.5"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.db_path, self.path)
        self.assertEqual(settings.token_minutes, 15)
        self.assertEqual(settings.sqlite_timeout, 0.5)
        self.assertNotIn(self.secret, repr(settings))

    def test_missing_database_rejects_startup_without_creating_it(self):
        app = create_app(Settings(self.path, self.secret))
        with self.assertRaises(DatabaseInitializationError):
            with TestClient(app):
                pass
        self.assertFalse(self.path.exists())

    def test_wrong_or_corrupt_database_is_not_migrated_or_rebuilt(self):
        self.path.write_bytes(b"not a database")
        before = self.path.read_bytes()
        with self.assertRaises(DatabaseInitializationError):
            with TestClient(create_app(Settings(self.path, self.secret))):
                pass
        self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
            connection.commit()
        finally:
            connection.close()
        before = self.path.read_bytes()
        with self.assertRaises(DatabaseInitializationError):
            with TestClient(create_app(Settings(self.path, self.secret))):
                pass
        self.assertEqual(self.path.read_bytes(), before)

    def test_mismatched_injected_database_is_rejected(self):
        with self.assertRaises(ValueError):
            create_app(Settings(self.path, self.secret), MedicineDB(self.root / "other.sqlite3"))

    def test_normal_startup_checks_database_and_preserves_it(self):
        db = MedicineDB(self.path)
        db.init_db()
        before = self.path.read_bytes()
        with TestClient(create_app(Settings(self.path, self.secret))) as client:
            self.assertEqual(client.get("/docs").status_code, 200)
        self.assertEqual(self.path.read_bytes(), before)

    def test_backend_contains_no_business_sql_and_core_hashes_are_unchanged(self):
        root = Path(__file__).resolve().parents[1]
        expected = {
            "schema.sql": "570b84f8d6ecded1e68e797c68baaf536ff82f8a37856c26d5ee9b0b4e9dedc0",
            "queries.sql": "ec4c213ea538c8a9eac7b35321a9220526f2574568759d20e7b1763d587c3d2a",
            # 授权增加日志迭代读取与CSV导出；保留完整性断言并更新对应基准。
            "medicine_db.py": "fe2e6d9c42d642ce421aad2ad00c0f0f379020ccbe88de34e2454d412b91b2f7",
            "data_io.py": "f3d989334fab8c9b20122456f9b69713abcb1537363fb7b4bb9e93ae26486f88",
            "test_medicine_db.py": "50b5f4e0e07e3330ffb063a9c691c9fc4120ea37a061d0b425a11314a3db72c2",
            "test_data_io.py": "2502c64f2cd7c4f490a2b2c16fc2dfecce03c41ae9c9abafae0f8ad5d33f10a3",
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest)
        for path in (root / "backend").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, ("execute", "executescript", "connect"), path.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
