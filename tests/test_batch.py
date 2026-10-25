"""批量登记：全部使用临时 SQLite；中途插入、日志及实际提交失败均须整批回滚。"""
import secrets
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from backend.auth import create_access_token
from backend.config import Settings
from backend.main import create_app
from medicine_db import MedicineDB


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='medicine-batch-test-')
        self.addCleanup(self.temp.cleanup)
        self.db = MedicineDB(Path(self.temp.name) / 'test.sqlite3', today_provider=lambda: date(2026, 10, 23), timeout=2)
        self.db.init_db()
        password = secrets.token_urlsafe(24)
        self.admin = self.db.create_first_admin('fictional_admin', password)['id']
        self.staff = self.db.create_user(self.admin, 'fictional_staff', password, 'staff')['id']
        self.viewer = self.db.create_user(self.admin, 'fictional_viewer', password, 'viewer')['id']
        self.settings = Settings(self.db.path, secrets.token_urlsafe(48))
        self.client = self.enterContext(TestClient(create_app(self.settings, self.db), raise_server_exceptions=False))
        self.headers = {role: {'Authorization': 'Bearer ' + create_access_token(uid, self.settings)['access_token']}
                        for role, uid in [('admin', self.admin), ('staff', self.staff), ('viewer', self.viewer)]}
        self.body = {'name': '虚构批量药品', 'location': '虚构急救柜A', 'expires_on': '2027-12-31', 'quantity': 10}

    def request(self, body=None, role='admin'):
        return self.client.post('/api/medicines/batch', headers=self.headers[role], json=self.body if body is None else body)

    def counts(self):
        with self.db.connection() as c:
            return tuple(c.execute('SELECT (SELECT COUNT(*) FROM medicines), (SELECT COUNT(*) FROM operation_logs)').fetchone())

    def assert_created(self, result, count):
        self.assertEqual(result['created_count'], count)
        rows = result['medicines']; self.assertEqual(len(rows), count)
        self.assertEqual(len({row['id'] for row in rows}), count)
        self.assertEqual(len({row['nfc_uuid'] for row in rows}), count)
        for row in rows:
            self.assertEqual((row['name'], row['location'], row['expires_on'], row['status'], row['is_active']),
                             (self.body['name'], self.body['location'], self.body['expires_on'], 'in_stock', 1))
        self.assertEqual(self.counts(), (count, count))
        logs = self.db.get_operation_logs(self.admin)
        self.assertEqual({log['medicine_id'] for log in logs}, {row['id'] for row in rows})
        self.assertTrue(all(log['action'] == 'register' and log['quantity'] == 1 and log['user_id'] == self.admin for log in logs))
        self.assertEqual(self.db.get_stock_for_name(self.body['name'], self.admin), {'name': self.body['name'], 'stock': count, 'available_stock': count})

    def test_batch_one(self):
        response = self.request({**self.body, 'quantity': 1}); self.assertEqual(response.status_code, 201, response.text)
        self.assert_created(response.json()['data'], 1)

    def test_batch_ten_unique_ids_uuids_and_logs(self):
        response = self.request(); self.assertEqual(response.status_code, 201, response.text)
        self.assert_created(response.json()['data'], 10)

    def test_batch_hundred_upper_boundary(self):
        response = self.request({**self.body, 'quantity': 100}); self.assertEqual(response.status_code, 201, response.text)
        self.assert_created(response.json()['data'], 100)

    def test_http_strict_quantity_validation_does_not_write(self):
        before = self.db.path.read_bytes()
        for value in (0, -1, 101, 1.5, '10', True, False, None, 10.0):
            with self.subTest(value=value): self.assertEqual(self.request({**self.body, 'quantity': value}).status_code, 422)
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_business_strict_quantity_validation_does_not_write(self):
        before = self.db.path.read_bytes()
        for value in (0, -1, 101, 1.5, '10', True, False, None, 10.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.db.add_medicines_batch('药品', '2027-12-31', value, self.admin)
        self.assertEqual(self.db.path.read_bytes(), before)

    def test_http_and_business_reuse_detail_validation(self):
        for key, value in [('name', ''), ('name', '   '), ('location', ''), ('expires_on', '2027-02-30'), ('expires_on', '20271231')]:
            with self.subTest(key=key,value=value): self.assertEqual(self.request({**self.body, key: value}).status_code, 422)
        for args in [('', '2027-12-31', '柜'), ('名称', '2027-02-30', '柜'), ('名称', '2027-12-31', '')]:
            with self.assertRaises(ValueError): self.db.add_medicines_batch(args[0], args[1], 2, self.admin, args[2])
        self.assertEqual(self.counts(), (0, 0))

    def test_defaults_quantity_and_location(self):
        response = self.request({'name': self.body['name'], 'expires_on': self.body['expires_on']})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['data']['created_count'], 1)
        self.assertEqual(response.json()['data']['medicines'][0]['location'], '未指定')

    def test_only_admin_can_register_batch(self):
        for role, uid in [('staff', self.staff), ('viewer', self.viewer)]:
            self.assertEqual(self.request(role=role).status_code, 403)
            with self.assertRaises(PermissionError): self.db.add_medicines_batch('药品', '2027-12-31', 10, uid)
        self.assertEqual(self.client.post('/api/medicines/batch', json=self.body).status_code, 401)
        self.assertEqual(self.counts(), (0, 0))

    def test_forbidden_fields_and_queries_cannot_inject_uuid_or_actor(self):
        for key, value in [('nfc_uuid', str(uuid4())), ('id', 10), ('user_id', self.staff), ('status', 'dispensed'), ('is_active', 0)]:
            self.assertEqual(self.request({**self.body, key: value}).status_code, 422)
        self.assertEqual(self.client.post('/api/medicines/batch?user_id=1', headers=self.headers['admin'], json=self.body).status_code, 422)
        self.assertEqual(self.counts(), (0, 0))

    def test_third_medicine_uuid_collision_rolls_back_entire_batch(self):
        first = uuid4()
        with patch('medicine_db.uuid4', side_effect=[first, uuid4(), first]):
            response = self.request()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.counts(), (0, 0))

    def test_third_registration_log_check_failure_rolls_back_entire_batch(self):
        original = self.db._execute; calls = 0
        def execute(conn, name, parameters=None):
            nonlocal calls
            if name == 'add_operation_log':
                calls += 1
                if calls == 3: parameters = {**parameters, 'quantity': 0}
            return original(conn, name, parameters)
        with patch.object(self.db, '_execute', side_effect=execute): response = self.request()
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(calls, 3); self.assertEqual(self.counts(), (0, 0))

    def test_real_commit_lock_failure_rolls_back_every_insert_and_log(self):
        self.db.timeout = 0.02
        reader = self.db._connect(); reader.execute('BEGIN'); reader.execute('SELECT COUNT(*) FROM medicines').fetchone()
        original = self.db._execute; inserted = []
        def execute(conn, name, parameters=None):
            cursor = original(conn, name, parameters)
            if name == 'add_operation_log': inserted.append(parameters['medicine_id'])
            return cursor
        try:
            with patch.object(self.db, '_execute', side_effect=execute): response = self.request({**self.body, 'quantity': 3})
        finally: reader.rollback(); reader.close()
        self.assertEqual(len(inserted), 3)  # 三行及日志确实写过，失败发生于 COMMIT 升级排他锁。
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers['retry-after'], '1'); self.assertEqual(self.counts(), (0, 0))

    def test_begin_lock_conflict_uses_existing_503_handling(self):
        self.db.timeout = .02
        with self.db.connection(write=True): response = self.request()
        self.assertEqual(response.status_code, 503); self.assertEqual(self.counts(), (0, 0))

    def test_two_administrators_concurrently_register_independent_batches(self):
        other = self.db.create_user(self.admin, 'fictional_second_admin', secrets.token_urlsafe(24), 'admin')['id']
        gate = threading.Barrier(2)
        def register(uid):
            gate.wait(10)
            return self.db.add_medicines_batch(self.body['name'], self.body['expires_on'], 10, uid, self.body['location'])
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(register, [self.admin, other]))
        rows = [r for result in results for r in result['medicines']]
        self.assertEqual(len(rows), 20); self.assertEqual(len({r['id'] for r in rows}), 20); self.assertEqual(len({r['nfc_uuid'] for r in rows}), 20)
        self.assertEqual(self.counts(), (20, 20)); self.assertEqual({log['user_id'] for log in self.db.get_operation_logs(self.admin)}, {self.admin, other})

    def test_one_write_transaction_and_no_independent_single_registration_calls(self):
        original = self.db._connect; traces = []
        def connect(*args, **kwargs):
            conn = original(*args, **kwargs); conn.set_trace_callback(traces.append); return conn
        with patch.object(self.db, '_connect', side_effect=connect), patch.object(self.db, 'add_medicine', side_effect=AssertionError('禁止循环独立提交')):
            result = self.db.add_medicines_batch('药品', '2027-12-31', 10, self.admin)
        self.assertEqual(result['created_count'], 10)
        self.assertEqual(sum(sql == 'BEGIN IMMEDIATE' for sql in traces), 1)
        self.assertEqual(sum(sql == 'COMMIT' for sql in traces), 1)

    def test_inventory_dashboard_and_scan_after_batch(self):
        self.assertEqual(self.request().status_code, 201)
        dashboard = self.client.get('/api/dashboard', headers=self.headers['viewer']).json()['data']
        self.assertEqual((dashboard['registered_count'], dashboard['dispensed_count']), (10, 0))
        self.assertEqual(dashboard['inventory'][0]['available_stock'], 10)
        scanned = self.client.post('/api/nfc/simulate', headers=self.headers['viewer'], json={'name': self.body['name']})
        self.assertEqual(scanned.status_code, 200); self.assertTrue(scanned.json()['data']['can_dispense']); self.assertEqual(self.counts(), (10, 10))

    def test_each_batch_uuid_still_dispenses_once(self):
        response = self.request(); self.assertEqual(response.status_code, 201, response.text)
        created = response.json()['data']['medicines']; scanned_uuids = set()
        for _ in created:
            scan = self.client.post('/api/nfc/simulate', headers=self.headers['staff'], json={'name': self.body['name']}).json()['data']
            body = {key: scan[key] for key in ('nfc_uuid', 'name', 'location', 'expires_on')}; scanned_uuids.add(scan['nfc_uuid'])
            self.assertEqual(self.client.post('/api/nfc/dispense', headers=self.headers['staff'], json=body).status_code, 200)
            self.assertEqual(self.client.post('/api/nfc/dispense', headers=self.headers['staff'], json=body).status_code, 409)
        self.assertEqual(scanned_uuids, {r['nfc_uuid'] for r in created})
        self.assertEqual(self.db.get_stock_for_name(self.body['name'], self.viewer)['stock'], 0)
        self.assertEqual(len(self.db.get_operation_logs(self.admin, action='dispense')), 10)

    def test_single_registration_contract_still_unchanged_and_static_route_order(self):
        body = {key: self.body[key] for key in ('name', 'location', 'expires_on')}
        response = self.client.post('/api/medicines', headers=self.headers['admin'], json=body)
        self.assertEqual(response.status_code, 201); row = response.json()['data']
        self.assertEqual(set(row), {'id','nfc_uuid','name','location','expires_on','status','is_active'})
        self.assertEqual(self.client.get('/api/medicines/'+str(row['id']), headers=self.headers['viewer']).status_code, 200)
        from backend.api import router
        paths = [route.path for route in router.routes]
        self.assertLess(paths.index('/api/medicines/batch'), paths.index('/api/medicines/{medicine_id}'))
