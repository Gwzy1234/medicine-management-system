"""前端同源服务与真实 HTTP 集成：仅操作 TemporaryDirectory 内的 SQLite。"""

import hashlib
import re
import secrets
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app
from medicine_db import BASE_DIR, MedicineDB


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="medicine-frontend-test-")
        self.addCleanup(self.temporary.cleanup)
        self.db = MedicineDB(Path(self.temporary.name) / "test.sqlite3")
        self.db.init_db()
        self.app = create_app(Settings(self.db.path, secrets.token_urlsafe(48)), self.db)
        self.client = self.enterContext(TestClient(self.app, raise_server_exceptions=False))

    def test_home_is_same_origin_html_and_has_content_security_policy(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn('lang="zh-CN"', response.text)
        self.assertIn('/static/js/app.js', response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("connect-src 'self'", response.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_all_local_resources_load_without_cdn(self):
        for path in ("css/style.css", "js/api.js", "js/auth.js", "js/ui.js", "js/views.js", "js/logs.js", "js/app.js", "icons.svg", "mark.svg"):
            with self.subTest(path=path):
                response = self.client.get("/static/" + path)
                self.assertEqual(response.status_code, 200)
                self.assertGreater(len(response.content), 30)
        html = self.client.get("/").text
        self.assertNotRegex(html, r'(?:src|href)="https?://')

    def test_docs_redoc_and_eighteen_api_contracts_remain_available(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 200)
        schema = self.client.get('/openapi.json').json()
        methods = [(path, method) for path, details in schema['paths'].items() for method in details if method in ('get', 'post', 'patch')]
        self.assertEqual(len(methods), 21)
        self.assertIn(("/api/export/logs", "get"), methods)
        self.assertIn(("/api/medicines/batch", "post"), methods)
        self.assertIn(("/api/dashboard", "get"), methods)
        self.assertTrue(all(path.startswith('/api/') for path, _ in methods))
        self.assertIn('DispenseOut', schema['components']['schemas'])

    def test_database_backend_and_test_files_cannot_be_downloaded(self):
        for path in ("/medical.db", "/schema.sql", "/medicine_db.py", "/backend/main.py", "/tests/frontend/index.html",
                     "/static/../medical.db", "/static/%2e%2e/medical.db", "/static/%2e%2e/backend/main.py", "/__tests/index.html"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn(response.status_code, (400, 404))
                self.assertNotIn(b'SQLite format', response.content)
        self.assertEqual(self.client.get('/api/inventory').status_code, 401)

    def test_static_requests_do_not_modify_database_or_create_users(self):
        before = self.db.path.read_bytes()
        for path in ('/', '/static/js/app.js', '/static/css/style.css', '/static/icons.svg'):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.db.path.read_bytes(), before)
        with self.db.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)

    def test_production_frontend_contains_no_unsafe_html_or_persistent_credentials(self):
        for path in (BASE_DIR / 'frontend/js').glob('*.js'):
            text = path.read_text()
            # 注释可以解释边界；实际代码不能使用危险 HTML 注入或持久化令牌。
            self.assertNotRegex(text, r'\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML\s*\(')
            self.assertNotRegex(text, r'(?:localStorage|sessionStorage)\s*\.')
            self.assertNotIn('Fictional-browser-only', text)
            self.assertNotRegex(text, r'console\.(?:log|debug)\s*\(')

    def test_sql_and_protected_business_files_keep_approved_hashes(self):
        expected = {
            'schema.sql': '570b84f8d6ecded1e68e797c68baaf536ff82f8a37856c26d5ee9b0b4e9dedc0',
            'queries.sql': 'ec4c213ea538c8a9eac7b35321a9220526f2574568759d20e7b1763d587c3d2a',
            'medicine_db.py': 'fe2e6d9c42d642ce421aad2ad00c0f0f379020ccbe88de34e2454d412b91b2f7',
            'data_io.py': 'f3d989334fab8c9b20122456f9b69713abcb1537363fb7b4bb9e93ae26486f88',
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((BASE_DIR / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(len(re.findall(r'^-- name:', (BASE_DIR / 'queries.sql').read_text(), re.M)), 29)

    def test_same_origin_frontend_with_real_login_register_edit_scan_cancel_and_dispense(self):
        admin = self.db.create_first_admin('fictional_frontend_admin', 'Fictional-frontend-password')
        self.assertEqual(self.client.get('/').status_code, 200)
        login = self.client.post('/api/auth/login', json={'username': admin['username'], 'password': 'Fictional-frontend-password'})
        self.assertEqual(login.status_code, 200)
        headers = {'Authorization': 'Bearer ' + login.json()['data']['access_token']}
        self.assertEqual(self.client.get('/api/auth/me', headers=headers).status_code, 200)
        created = self.client.post('/api/medicines', headers=headers, json={'name': '虚构前端药品', 'location': '虚构CT室', 'expires_on': '2035-01-01'})
        self.assertEqual(created.status_code, 201)
        row = created.json()['data']
        edited = self.client.patch(f'/api/medicines/{row["id"]}', headers=headers, json={'name': '虚构前端药品', 'location': '虚构MRI室', 'expires_on': '2035-02-01'})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()['data']['nfc_uuid'], row['nfc_uuid'])
        before = self.db.path.read_bytes()
        scanned = self.client.post('/api/nfc/simulate', headers=headers, json={'name': row['name']})
        self.assertEqual(scanned.status_code, 200)
        self.assertEqual(self.client.post('/api/nfc/scan', headers=headers, json={'nfc_uuid': row['nfc_uuid']}).status_code, 200)
        self.assertEqual(self.db.path.read_bytes(), before)  # 取消就是不发送领取请求。
        self.assertEqual(self.client.post('/api/nfc/dispense', headers=headers, json={key: edited.json()['data'][key] for key in ('nfc_uuid', 'name', 'location', 'expires_on')}).status_code, 200)
        self.assertEqual(self.client.post('/api/nfc/dispense', headers=headers, json={key: edited.json()['data'][key] for key in ('nfc_uuid', 'name', 'location', 'expires_on')}).status_code, 409)
        stock = self.client.get('/api/inventory/by-name', headers=headers, params={'name': row['name']}).json()['data']
        self.assertEqual((stock['stock'], stock['available_stock']), (0, 0))
        logs = self.client.get('/api/logs', headers=headers, params={'action': 'dispense'}).json()['data']
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['medicine_id'], row['id'])
        exported = self.client.get('/api/export/medicines', headers=headers)
        self.assertEqual(exported.status_code, 200)
        self.assertTrue(exported.content.startswith(b'\xef\xbb\xbf'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
