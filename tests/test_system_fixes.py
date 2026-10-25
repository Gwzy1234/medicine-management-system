"""B1～B5 正式回归：真实临时 SQLite；并发测试只控制执行顺序，不替代事务。"""
import secrets
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend.auth import create_access_token
from backend.config import Settings
from backend.main import create_app
from medicine_db import MedicineDB

PASSWORD = 'Fictional-system-fix-password'


def confirmation(row):
    return {key: row[key] for key in ('nfc_uuid', 'name', 'location', 'expires_on')}


class SystemFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='medicine-system-fix-')
        self.addCleanup(self.temp.cleanup)
        self.today = date(2026, 10, 23)
        self.db = MedicineDB(Path(self.temp.name) / 'test.sqlite3', today_provider=lambda: self.today, timeout=2)
        self.db.init_db()
        self.admin = self.db.create_first_admin('fictional_admin', PASSWORD)['id']
        self.staff = self.db.create_user(self.admin, 'fictional_staff', PASSWORD, 'staff')['id']
        self.viewer = self.db.create_user(self.admin, 'fictional_viewer', PASSWORD, 'viewer')['id']
        self.settings = Settings(self.db.path, secrets.token_urlsafe(48))
        self.client = self.enterContext(TestClient(create_app(self.settings, self.db), raise_server_exceptions=False))
        self.headers = {role: {'Authorization': 'Bearer ' + create_access_token(uid, self.settings)['access_token']}
                        for role, uid in [('admin', self.admin), ('staff', self.staff), ('viewer', self.viewer)]}
        self.row = self.db.add_medicine('虚构确认药品', '2030-01-01', self.admin, '虚构柜')

    def request(self, method, path, role='admin', **kwargs):
        return self.client.request(method, path, headers=self.headers[role], **kwargs)

    def dispense(self, row=None, role='staff'):
        return self.request('POST', '/api/nfc/dispense', role, json=confirmation(row or self.row))

    def dispense_logs(self):
        return self.db.get_operation_logs(self.admin, medicine_id=self.row['id'], action='dispense')

    def test_original_eighteen_routes_preserved_plus_one_dashboard(self):
        expected = {
            ('post', '/api/auth/login'), ('get', '/api/auth/me'),
            ('get', '/api/users'), ('post', '/api/users'), ('get', '/api/users/{user_id}'),
            ('get', '/api/medicines'), ('post', '/api/medicines'),
            ('get', '/api/medicines/{medicine_id}'), ('patch', '/api/medicines/{medicine_id}'),
            ('post', '/api/medicines/{medicine_id}/enable'), ('post', '/api/medicines/{medicine_id}/disable'),
            ('get', '/api/inventory'), ('get', '/api/inventory/by-name'),
            ('post', '/api/nfc/scan'), ('post', '/api/nfc/simulate'), ('post', '/api/nfc/dispense'),
            ('get', '/api/logs'), ('get', '/api/export/medicines'),
        }
        schema = self.client.get('/openapi.json').json()
        actual = {(method, path) for path, methods in schema['paths'].items()
                  for method in methods if method in ('get', 'post', 'patch')}
        self.assertEqual(actual, expected | {('get', '/api/dashboard'), ('post', '/api/medicines/batch'),
                                             ('get', '/api/export/logs')})

    def test_b1_each_changed_detail_rejects_old_confirmation_then_rescan_succeeds(self):
        for field, value in [('name', '虚构新名称'), ('location', '另一虚构柜'), ('expires_on', '2031-02-03')]:
            with self.subTest(field=field):
                row = self.db.add_medicine('原名称', '2030-01-01', self.admin)
                new = {key: row[key] for key in ('name', 'location', 'expires_on')}; new[field] = value
                self.db.update_medicine_details(row['id'], **new, user_id=self.admin)
                before = self.db.path.read_bytes()
                response = self.dispense(row)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()['error']['code'], 'details_changed')
                self.assertEqual(self.db.path.read_bytes(), before)
                latest = self.request('POST', '/api/nfc/scan', json={'nfc_uuid': row['nfc_uuid']}).json()['data']
                self.assertEqual(self.dispense(latest).status_code, 200)
                self.assertEqual(self.dispense(latest).status_code, 409)

    def test_b1_uuid_only_http_request_is_explicitly_rejected(self):
        before = self.db.path.read_bytes()
        for role in ('admin', 'staff'):
            self.assertEqual(self.request('POST', '/api/nfc/dispense', role, json={'nfc_uuid': self.row['nfc_uuid']}).status_code, 422)
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_b1_confirmation_fields_required_and_validated(self):
        for field in ('name', 'location', 'expires_on'):
            for value in (None, '', '   '):
                body = confirmation(self.row); body[field] = value
                self.assertEqual(self.request('POST', '/api/nfc/dispense', json=body).status_code, 422)
        body = confirmation(self.row); body['expires_on'] = '2030-02-30'
        self.assertEqual(self.request('POST', '/api/nfc/dispense', json=body).status_code, 422)
        self.assertEqual(self.dispense_logs(), [])

    def test_b1_business_guard_runs_after_write_lock_and_reads_actual_record(self):
        original = self.db._execute; seen = []
        def execute(conn, name, parameters=None):
            if name == 'get_medicine_by_nfc':
                # 独立连接必须拿不到写锁，说明资料比较确实受 BEGIN IMMEDIATE 保护。
                other = self.db._connect(); other.execute('PRAGMA busy_timeout=0')
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                        other.execute('BEGIN IMMEDIATE')
                finally:
                    other.close()
                seen.append(name)
            return original(conn, name, parameters)
        with patch.object(self.db, '_execute', side_effect=execute):
            result = self.db.withdraw_by_nfc(self.row['nfc_uuid'], self.staff,
                                           confirmed_details={k: self.row[k] for k in ('name', 'location', 'expires_on')})
        self.assertTrue(result['success']); self.assertTrue(seen)
        self.assertEqual(len(self.dispense_logs()), 1)

    def test_b1_log_failure_rolls_back_confirmed_dispense(self):
        original = self.db._execute
        def execute(conn, name, parameters=None):
            if name == 'add_operation_log':
                raise RuntimeError('fictional log failure')
            return original(conn, name, parameters)
        before = self.db.path.read_bytes()
        with patch.object(self.db, '_execute', side_effect=execute):
            self.assertEqual(self.dispense().status_code, 500)
        self.assertEqual(self.db.path.read_bytes(), before)
        self.assertEqual(self.dispense_logs(), [])

    def test_b1_confirmed_concurrent_http_dispense_succeeds_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda _: self.dispense().status_code, range(8)))
        self.assertEqual(sorted(codes), [200] + [409] * 7)
        self.assertEqual(len(self.dispense_logs()), 1)

    def test_b1_permission_and_expired_disabled_guards_remain(self):
        self.assertEqual(self.dispense(role='viewer').status_code, 403)
        self.db.disable_medicine(self.row['id'], self.admin)
        self.assertEqual(self.dispense().json()['error']['code'], 'disabled')
        self.db.enable_medicine(self.row['id'], self.admin)
        self.today = date(2030, 1, 2)
        self.assertEqual(self.dispense().json()['error']['code'], 'expired')
        self.assertEqual(self.dispense_logs(), [])

    def assert_dashboard_snapshot(self, mutation):
        conn = self.db._connect()
        try: conn.execute('PRAGMA journal_mode=WAL')
        finally: conn.close()
        read = threading.Event(); changed = threading.Event(); original = self.db._execute
        def execute(conn, name, parameters=None):
            cursor = original(conn, name, parameters)
            if name == 'get_stock_by_name':
                rows = cursor.fetchall(); read.set()
                if not changed.wait(10): raise RuntimeError('writer did not finish')
                return rows
            return cursor
        with patch.object(self.db, '_execute', side_effect=execute):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.request, 'GET', '/api/dashboard')
                try:
                    self.assertTrue(read.wait(10)); mutation()
                finally: changed.set()
                response = pending.result(10)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()['data']
        self.assertEqual(data['inventory'], [{'name': self.row['name'], 'stock': 1, 'available_stock': 1}])
        self.assertEqual(data['medicines'], [self.row])
        self.assertEqual((data['registered_count'], data['dispensed_count']), (1, 0))
        latest = self.request('GET', '/api/dashboard').json()['data']
        self.assertNotEqual(latest, data)
        self.assertEqual(sum(r['stock'] for r in latest['inventory']) + latest['dispensed_count'], latest['registered_count'])

    def test_b2_dashboard_snapshot_during_concurrent_dispense(self):
        self.assert_dashboard_snapshot(lambda: self.db.withdraw_by_nfc(self.row['nfc_uuid'], self.staff))

    def test_b2_dashboard_snapshot_during_concurrent_register(self):
        self.assert_dashboard_snapshot(lambda: self.db.add_medicine('新登记', '2030-01-01', self.admin))

    def test_b2_dashboard_snapshot_during_concurrent_edit(self):
        self.assert_dashboard_snapshot(lambda: self.db.update_medicine_details(self.row['id'], '新名称', '新柜', '2020-01-01', self.admin))

    def test_b2_dashboard_empty_permissions_and_read_only(self):
        before = self.db.path.read_bytes()
        for role in self.headers:
            response = self.request('GET', '/api/dashboard', role)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['data']['registered_count'], 1)
        self.assertEqual(self.client.get('/api/dashboard').status_code, 401)
        self.assertEqual(self.request('GET', '/api/dashboard?today=2000-01-01').status_code, 422)
        self.assertEqual(self.db.path.read_bytes(), before)
        with tempfile.TemporaryDirectory() as tmp:
            db = MedicineDB(Path(tmp) / 'empty.sqlite3'); db.init_db()
            uid = db.create_first_admin('empty', PASSWORD)['id']
            result = db.get_dashboard(uid)
            self.assertEqual((result['inventory'], result['medicines'], result['registered_count'], result['dispensed_count']), ([], [], 0, 0))

    def test_b4_every_frontend_html_entry_has_same_security_headers(self):
        home = self.client.get('/')
        for method, path in [('GET', '/static/index.html'), ('HEAD', '/static/index.html'), ('GET', '/static/index.html?cache=1')]:
            response = self.client.request(method, path)
            self.assertEqual(response.status_code, 200)
            for key in ('content-security-policy', 'x-content-type-options', 'cache-control'):
                self.assertEqual(response.headers.get(key), home.headers[key], (path, key))
            self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])

    def test_b4_assets_and_documentation_remain_available(self):
        for path in ('/static/js/app.js', '/static/css/style.css', '/static/icons.svg', '/docs', '/redoc', '/openapi.json'):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertNotIn('content-security-policy', self.client.get('/docs').headers)

    def test_b5_scan_returns_server_date_and_eligibility_for_all_roles(self):
        for role in self.headers:
            response = self.request('POST', '/api/nfc/scan', role, json={'nfc_uuid': self.row['nfc_uuid']})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['data']['business_date'], '2026-10-23')
            self.assertIs(response.json()['data']['can_dispense'], True)
        self.db.disable_medicine(self.row['id'], self.admin)
        self.assertIs(self.request('POST', '/api/nfc/scan', json={'nfc_uuid': self.row['nfc_uuid']}).json()['data']['can_dispense'], False)

    def test_b5_expiry_day_valid_midnight_rechecked(self):
        self.today = date(2030, 1, 1)
        for path, body in [('/api/nfc/scan', {'nfc_uuid': self.row['nfc_uuid']}), ('/api/nfc/simulate', {'name': self.row['name']})]:
            scanned = self.request('POST', path, json=body).json()['data']
            self.assertIs(scanned['can_dispense'], True)
            self.assertEqual(scanned['business_date'], '2030-01-01')
        self.today = date(2030, 1, 2)
        self.assertEqual(self.dispense().json()['error']['code'], 'expired')
        self.assertIs(self.request('POST', '/api/nfc/scan', json={'nfc_uuid': self.row['nfc_uuid']}).json()['data']['can_dispense'], False)
        self.assertEqual(self.dispense_logs(), [])

    def test_b5_expiry_edit_after_scan_requires_new_confirmation(self):
        self.db.update_medicine_details(self.row['id'], self.row['name'], self.row['location'], '2020-01-01', self.admin)
        self.assertEqual(self.dispense().json()['error']['code'], 'details_changed')
        latest = self.request('POST', '/api/nfc/scan', json={'nfc_uuid': self.row['nfc_uuid']}).json()['data']
        self.assertIs(latest['can_dispense'], False)
        self.assertEqual(self.dispense(latest).json()['error']['code'], 'expired')
        self.assertEqual(self.dispense_logs(), [])

    def test_b5_client_date_parameters_cannot_override_server(self):
        for path, body in [('/api/nfc/scan', {'nfc_uuid': self.row['nfc_uuid']}), ('/api/nfc/simulate', {'name': self.row['name']}), ('/api/nfc/dispense', confirmation(self.row))]:
            for value in ('2026-10-21', '2026-10-25'):
                self.assertEqual(self.request('POST', path, json={**body, 'today': value}).status_code, 422)
        self.assertEqual(self.dispense_logs(), [])
