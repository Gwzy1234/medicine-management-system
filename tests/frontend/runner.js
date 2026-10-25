import { createClient, ApiError, query, medicineRecord, stockRecord, records } from '/static/js/api.js';
import { el, field, select, textInput, positiveFilter } from '/static/js/ui.js';
import { renderDashboard, renderMedicines, renderNFC } from '/static/js/views.js';
import { expired } from '/static/js/ui.js';
import { api } from '/static/js/api.js';
import { auth } from '/static/js/auth.js';
import { runLogTests } from './logs_cases.js';
import { runUITests } from './ui_cases.js';
import { runLogExportTests } from './log_export_cases.js';

let passed = 0, failed = 0;
const assert = (condition, message = '断言未满足') => { if (!condition) throw new Error(message); };
const response = (body, status = 200, type = 'application/json') => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': type } });
async function rejects(action, code, status) {
  try { await action(); } catch (error) {
    assert(error instanceof ApiError); assert(error.code === code, `${error.code} != ${code}`);
    if (status !== undefined) assert(error.status === status); return;
  }
  throw new Error('应拒绝但请求成功');
}
async function test(name, action) {
  try { await action(); passed++; document.getElementById('results').append(el('li', { text: `PASS · ${name}` })); }
  catch (error) { failed++; document.getElementById('results').append(el('li', { text: `FAIL · ${name} · ${error.message}` })); }
}

await test('统一解析 data 封装', async () => { assert((await createClient({ fetcher: async () => response({ data: { id: 1 } }) }).request('/api/test')).id === 1); });
await test('200 携带 error 不能视为成功', () => rejects(() => createClient({ fetcher: async () => response({ error: { message: 'failed' } }) }).request('/api/test'), 'contract_error'));
await test('非 JSON 成功响应不能视为成功', () => rejects(() => createClient({ fetcher: async () => new Response('broken') }).request('/api/test'), 'contract_error'));
await test('Bearer 只放请求头；不保存密码或令牌到 URL', async () => {
  let observed;
  const client = createClient({ fetcher: async (path, options) => { observed = [path, options]; return response({ data: [] }); } });
  client.setToken('unit-only-token'); await client.request('/api/test');
  assert(observed[0] === '/api/test'); assert(observed[1].headers.Authorization === 'Bearer unit-only-token');
  client.setToken(null); await client.request('/api/test'); assert(!observed[1].headers.Authorization);
});
await test('不向外部地址发送令牌', () => rejects(() => createClient().request('https://example.invalid/api/test'), 'contract_error'));
for (const status of [401, 403, 404, 409, 422, 500, 503]) {
  await test(`HTTP ${status} 明确抛出失败`, async () => {
    let logout = 0;
    const client = createClient({ fetcher: async () => response({ error: { code: `http_${status}`, message: '测试错误' } }, status) });
    client.onUnauthorized(() => { logout++; });
    await rejects(() => client.request('/api/test'), `http_${status}`, status);
    assert(logout === (status === 401 ? 1 : 0));
  });
}
await test('登录失败 401 不误触会话过期回调', async () => {
  let called = false;
  const client = createClient({ fetcher: async () => response({ error: { code: 'invalid_credentials' } }, 401) });
  client.onUnauthorized(() => { called = true; });
  await rejects(() => client.request('/api/auth/login', { auth: false }), 'invalid_credentials'); assert(!called);
});
await test('网络失败不重试写操作', async () => {
  let calls = 0;
  const client = createClient({ fetcher: async () => { calls++; throw new TypeError('offline'); } });
  await rejects(() => client.request('/api/nfc/dispense', { method: 'POST', body: { nfc_uuid: 'fake' } }), 'network_error'); assert(calls === 1);
});
await test('超时中止，并提示结果尚未确认', async () => {
  const client = createClient({ timeout: 5, fetcher: (path, options) => new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('abort', 'AbortError')))) });
  await rejects(() => client.request('/api/test'), 'timeout');
});
await test('切页中止与网络故障区别处理', async () => {
  const controller = new AbortController();
  const client = createClient({ fetcher: (path, options) => new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('abort', 'AbortError')))) });
  const pending = client.request('/api/test', { signal: controller.signal }); controller.abort();
  await rejects(() => pending, 'aborted');
});
await test('旧账号的延迟响应不覆盖新账号', async () => {
  let resolve;
  const client = createClient({ fetcher: () => new Promise(done => { resolve = done; }) });
  client.setToken('first'); const pending = client.request('/api/test'); client.setToken('second');
  resolve(response({ data: [] })); await rejects(() => pending, 'stale_session');
});
await test('旧账号的 401 不退出新账号', async () => {
  let resolve, called = false;
  const client = createClient({ fetcher: () => new Promise(done => { resolve = done; }) });
  client.onUnauthorized(() => { called = true; });
  const pending = client.request('/api/test'); client.setToken('second'); resolve(response({ error: {} }, 401));
  await rejects(() => pending, 'stale_session'); assert(!called);
});
await test('CSV 类型错误不能下载 JSON', () => rejects(() => createClient({ fetcher: async () => response({ data: [] }) }).request('/api/export/medicines', { file: true }), 'contract_error'));
await test('CSV BOM 原样保留', async () => {
  const bytes = new Uint8Array([239, 187, 191, 105, 100, 10]);
  const client = createClient({ fetcher: async () => new Response(bytes, { headers: { 'Content-Type': 'text/csv' } }) });
  const file = await client.request('/api/export/medicines', { file: true });
  assert(file.filename === 'medicines.csv'); assert(new Uint8Array(await file.blob.arrayBuffer())[0] === 239);
});
await test('查询参数编码且保留 days=0', () => { assert(query({ name: '药品 & A', days: 0, empty: '' }) === '?name=%E8%8D%AF%E5%93%81+%26+A&days=0'); });
await test('HTML 特殊字符保持文本，不能生成脚本节点', () => {
  const value = '<img src=x onerror=alert(1)>';
  const node = el('div', {}, value); assert(node.textContent === value); assert(node.querySelector('img') === null);
});
await test('下拉框标签不混入选项文字或提示', () => {
  const control = select('test', [['a', '甲'], ['b', '乙']]);
  const wrapper = field('选择药品', control, '提示');
  document.body.append(wrapper);
  try { assert(control.labels.length === 1); assert(control.labels[0].textContent === '选择药品'); }
  finally { wrapper.remove(); }
});
await test('500 个 Unicode 字符不被 UTF-16 maxlength 错误截断', () => {
  const input = textInput('username', '', '🩺'.repeat(500));
  assert(input.value.length === 1000); assert(input.validity.valid);
  input.value = 'a'.repeat(501); input.dispatchEvent(new Event('input')); assert(!input.validity.valid);
});
await test('64 位筛选 ID 不经过 Number 舍入', () => {
  assert(positiveFilter('9223372036854775807') === '9223372036854775807');
  for (const value of ['9223372036854775808', '-1', '1.5', '01']) { let rejected = false; try { positiveFilter(value); } catch { rejected = true; } assert(rejected); }
});
await test('拒绝不安全的药品 ID 与错误库存结构', () => {
  for (const action of [() => medicineRecord({ id: 2 ** 53 }), () => stockRecord({ name: 'A', stock: 1, available_stock: 2 }), () => records({})]) {
    let rejected = false; try { action(); } catch { rejected = true; } assert(rejected);
  }
});
await test('本地令牌到期清空登录状态', async () => {
  const original = window.fetch;
  window.fetch = async path => response({ data: path.includes('/login') ? { access_token: 'unit-only', expires_in: 0.01 } : { id: 1, username: 'unit-test', role: 'viewer' } });
  try {
    await auth.login('unit-test', 'unit-only-password'); assert(auth.user()?.role === 'viewer');
    await new Promise(resolve => setTimeout(resolve, 30)); assert(auth.user() === null);
  } finally { window.fetch = original; auth.logout(); }
});


// B1～B5：直接运行实际视图和浏览器原生表单；HTTP 事务另由 Python/端到端测试验证。
const fixture = { id: 1, nfc_uuid: 'fictional-unit-uuid', name: '虚构药品', location: '虚构柜', expires_on: '2030-01-01', status: 'in_stock', is_active: 1 };
function context(request, parameters = {}) {
  const root = el('main'); document.body.append(root);
  return { root, parameters, request, current: () => true, user: () => ({ id: 1, role: 'admin' }),
    navigate() {}, toast() {}, reload() {},
    async run(target, action) { try { return { ok: true, value: await action() }; } catch (error) { return { ok: false, error }; } },
  };
}
async function settled(check) {
  for (let i = 0; i < 100; i++) { if (check()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
  throw new Error('异步视图未完成');
}
async function nfcContext(row) {
  const ctx = context(async path => path === '/api/inventory' ? [{ name: row.name, stock: 1, available_stock: 1 }] : { ...row });
  await renderNFC(ctx);
  ctx.root.querySelector('select').value = row.name;
  ctx.root.querySelector('form').requestSubmit();
  await settled(() => ctx.root.textContent.includes('识别成功'));
  return ctx;
}
await test('B1 最终请求携带确认框中的三项资料及 UUID；409 清除旧结果', async () => {
  const row = { ...fixture, business_date: '2026-10-23', can_dispense: true };
  const ctx = await nfcContext(row); const original = api.request; let body;
  api.request = async (path, options) => { body = options.body; throw new ApiError('资料已变化，请重新扫描', 409, 'details_changed'); };
  try {
    [...ctx.root.querySelectorAll('button')].find(b => b.textContent === '确认领取').click();
    const modal = document.querySelector('dialog');
    [...modal.querySelectorAll('button')].find(b => b.textContent === '确认领取 1 支').click();
    await settled(() => !document.querySelector('dialog'));
    assert(JSON.stringify(body) === JSON.stringify({ nfc_uuid: row.nfc_uuid, name: row.name, location: row.location, expires_on: row.expires_on }));
    assert(ctx.root.textContent.includes('请重新扫描'));
    assert(ctx.root.textContent.includes('本次未确认领取成功')); assert(!ctx.root.querySelector('.alert.success'));
  } finally { api.request = original; document.querySelectorAll('dialog').forEach(d => { d.close(); d.remove(); }); ctx.root.remove(); }
});
await test('B2 总览仅请求一个快照，不再请求列表或库存拼接', async () => {
  const calls = [];
  const ctx = context(async path => {
    calls.push(path); assert(path === '/api/dashboard', path);
    return { business_date: '2026-10-23', inventory: [{ name: fixture.name, stock: 1, available_stock: 1 }], medicines: [fixture], registered_count: 1, dispensed_count: 0 };
  });
  try { await renderDashboard(ctx); assert(calls.length === 1); assert(ctx.root.textContent.includes('账面库存总数')); }
  finally { ctx.root.remove(); }
});
for (const value of ['-1', '3651', '1.5', '']) {
  await test(`B3 临期输入 ${JSON.stringify(value)} 隐藏后不阻止其他分类；参数不提交`, async () => {
    const ctx = context(async () => [fixture]); const navigated = [];
    ctx.navigate = (page, parameters) => navigated.push(parameters);
    try {
      await renderMedicines(ctx);
      const category = ctx.root.querySelector('select'); const days = ctx.root.querySelector('input[name=days]');
      category.value = 'expiring'; category.dispatchEvent(new Event('change'));
      assert(!days.disabled); days.value = value;
      category.value = 'in_stock'; category.dispatchEvent(new Event('change'));
      assert(days.disabled); assert(ctx.root.querySelector('form').checkValidity());
      ctx.root.querySelector('form').requestSubmit();
      assert(navigated.length === 1); assert(!Object.hasOwn(navigated[0], 'days'));
      category.value = 'expiring'; category.dispatchEvent(new Event('change')); assert(!days.disabled);
      assert(days.validity.valid === (value === '')); // 空值约定为 0 天；其余非法值不能提交临期查询。
    } finally { ctx.root.remove(); }
  });
}
await test('B3 快速切换分类后临期校验恢复，空值按 0 天提交', async () => {
  const ctx = context(async () => []); let sent;
  ctx.navigate = (page, parameters) => { sent = parameters; };
  try {
    await renderMedicines(ctx); const category = ctx.root.querySelector('select'); const days = ctx.root.querySelector('input[name=days]');
    for (let i = 0; i < 10; i++) for (const state of ['expiring', 'all']) { category.value = state; category.dispatchEvent(new Event('change')); }
    assert(days.disabled);
    category.value = 'expiring'; category.dispatchEvent(new Event('change')); days.value = '';
    ctx.root.querySelector('form').requestSubmit(); assert(sent.days === 0);
  } finally { ctx.root.remove(); }
});
await test('B5 缺少服务器日期时不从客户端时钟猜测过期状态', () => {
  assert(!expired({ ...fixture, expires_on: '2000-01-01' }));
});
for (const offset of [-2, 2]) {
  await test(`B5 客户端日期偏差 ${offset} 天，领取入口仍遵循服务端`, async () => {
    const RealDate = window.Date;
    const serverRow = { ...fixture, expires_on: '2026-10-23', business_date: '2026-10-23', can_dispense: true };
    window.Date = class extends RealDate { constructor(...args) { super(...(args.length ? args : [`2026-10-${23 + offset}T12:00:00+08:00`])); } };
    let ctx;
    try { ctx = await nfcContext(serverRow); assert(!expired(serverRow)); assert([...ctx.root.querySelectorAll('button')].some(b => b.textContent === '确认领取')); }
    finally { window.Date = RealDate; ctx?.root.remove(); }
  });
}
await test('B5 时区和午夜不改写服务端扫描资格，过期结果仍拒绝', async () => {
  const original = Intl.DateTimeFormat; let ctx;
  Intl.DateTimeFormat = function () { throw new Error('业务判断不应读取客户端时区'); };
  try {
    const row = { ...fixture, expires_on: '2026-10-23', business_date: '2026-10-24', can_dispense: false };
    ctx = await nfcContext(row); assert(expired(row));
    assert(![...ctx.root.querySelectorAll('button')].some(b => b.textContent === '确认领取'));
  } finally { Intl.DateTimeFormat = original; ctx?.root.remove(); }
});
for (const zone of ['America/Los_Angeles', 'Pacific/Kiritimati']) {
  await test(`B5 时区模拟 ${zone} 不影响服务器当天的领取资格`, async () => {
    const original = Intl.DateTimeFormat; let ctx;
    Intl.DateTimeFormat = function (locale, options) { return new original(locale, { ...options, timeZone: zone }); };
    try {
      const row = { ...fixture, expires_on: '2026-10-23', business_date: '2026-10-23', can_dispense: true };
      ctx = await nfcContext(row); assert(!expired(row));
      assert([...ctx.root.querySelectorAll('button')].some(b => b.textContent === '确认领取'));
    } finally { Intl.DateTimeFormat = original; ctx?.root.remove(); }
  });
}


// 第八阶段：批量表单的原生验证、二次确认、重复点击及不确定结果保护。
async function batchContext() {
  const ctx = context(async () => []); ctx.toasts = []; ctx.navigations = [];
  ctx.toast = message => ctx.toasts.push(message);
  ctx.navigate = (page, parameters) => ctx.navigations.push({ page, parameters });
  await renderMedicines(ctx);
  const entry = [...ctx.root.querySelectorAll('button')].find(b => b.textContent === '批量登记药品');
  assert(entry, '缺少批量登记入口'); entry.click();
  const modal = document.querySelector('dialog'); const form = modal.querySelector('form');
  return { ctx, modal, form, name: form.elements.name, location: form.elements.location,
    expiry: form.elements.expires_on, quantity: form.elements.quantity };
}
function batchValues(state, quantity = '10') {
  state.name.value = fixture.name; state.location.value = fixture.location;
  state.expiry.value = fixture.expires_on; state.quantity.value = quantity;
}
function batchCleanup(state) {
  document.querySelectorAll('dialog').forEach(d => { d.close(); d.remove(); }); state?.ctx.root.remove();
}
function batchResponse(quantity) {
  return { created_count: quantity, medicines: Array.from({ length: quantity }, (_, i) => ({ ...fixture, id: i + 1, nfc_uuid: `fictional-batch-${i}` })) };
}
await test('批量登记：管理员同时保留单支和批量入口，其他角色无入口', async () => {
  for (const role of ['admin', 'staff', 'viewer']) {
    const ctx = context(async () => []); ctx.user = () => ({ id: 1, role });
    try {
      await renderMedicines(ctx);
      assert([...ctx.root.querySelectorAll('button')].some(b => b.textContent === '登记药品') === (role === 'admin'));
      assert([...ctx.root.querySelectorAll('button')].some(b => b.textContent === '批量登记药品') === (role === 'admin'));
    } finally { ctx.root.remove(); }
  }
});
await test('批量登记：默认数量1，摘要确认前不发送写请求，返回修改保留内容', async () => {
  let state; const original = api.request; let calls = 0; api.request = async () => { calls++; };
  try {
    state = await batchContext(); assert(state.quantity.value === '1'); batchValues(state, '1');
    state.form.requestSubmit(); assert(calls === 0); assert(state.modal.textContent.includes('即将登记1支'));
    [...state.modal.querySelectorAll('button')].find(b => b.textContent === '返回修改').click();
    assert(!state.quantity.disabled); assert(state.name.value === fixture.name); assert(state.quantity.value === '1');
  } finally { api.request = original; batchCleanup(state); }
});
await test('批量登记：0、负数、101、小数和空值均不能进入确认', async () => {
  let state;
  try {
    state = await batchContext();
    for (const value of ['0','-1','101','1.5','']) {
      batchValues(state, value); assert(!state.form.checkValidity(), value);
      state.form.requestSubmit(); assert(!state.name.disabled); assert(!state.modal.textContent.includes('即将登记'));
    }
  } finally { batchCleanup(state); }
});
await test('批量登记：重复点击只产生一个请求，成功数量来自后端并重新查询列表', async () => {
  let state, resolve, calls = 0; const original = api.request;
  api.request = (path, options) => {
    assert(path === '/api/medicines/batch'); assert(options.body.quantity === 10); calls++;
    return new Promise(done => { resolve = done; });
  };
  try {
    state = await batchContext(); batchValues(state); state.form.requestSubmit();
    assert(state.modal.textContent.includes('即将登记10支')); assert(state.name.disabled);
    const submit = state.form.querySelector('button[type=submit]'); submit.click(); submit.click();
    assert(calls === 1); assert([...state.modal.querySelectorAll('button')].find(b => b.textContent === '取消').disabled);
    resolve(batchResponse(10)); await settled(() => !document.querySelector('dialog'));
    assert(state.ctx.toasts.some(t => t.includes('10'))); assert(state.ctx.navigations.length === 1);
    assert(state.ctx.navigations[0].page === 'medicines'); assert(state.ctx.navigations[0].parameters.name === fixture.name);
  } finally { api.request = original; batchCleanup(state); }
});
await test('批量登记：明确失败保留输入，不显示部分成功或自动重试', async () => {
  let state, calls = 0; const original = api.request;
  api.request = async () => { calls++; throw new ApiError('参数不合法', 422, 'validation_error'); };
  try {
    state = await batchContext(); batchValues(state); state.form.requestSubmit(); state.form.requestSubmit();
    await settled(() => state.modal.textContent.includes('参数不合法'));
    assert(calls === 1); assert(state.name.value === fixture.name); assert(state.quantity.value === '10');
    assert(!state.quantity.disabled); assert(state.ctx.toasts.length === 0); assert(state.ctx.navigations.length === 0);
  } finally { api.request = original; batchCleanup(state); }
});
await test('批量登记：响应丢失提示结果未知，阻止原表单再次写入并提供查询入口', async () => {
  let state, calls = 0; const original = api.request;
  api.request = async () => { calls++; throw new ApiError('结果尚未确认，请刷新核实', 0, 'timeout'); };
  try {
    state = await batchContext(); batchValues(state); state.form.requestSubmit(); state.form.requestSubmit();
    await settled(() => [...state.modal.querySelectorAll('button')].some(b => b.textContent === '刷新核实登记结果' && !b.hidden));
    state.form.requestSubmit(); assert(calls === 1); assert(state.form.querySelector('button[type=submit]').disabled);
    assert(state.name.disabled); assert(state.quantity.disabled);
    [...state.modal.querySelectorAll('button')].find(b => b.textContent === '刷新核实登记结果').click();
    assert(state.ctx.navigations.length === 1); assert(state.ctx.navigations[0].parameters.name === fixture.name);
    assert(state.ctx.toasts.length === 0);
  } finally { api.request = original; batchCleanup(state); }
});
await test('批量登记：部分数量或重复UUID的异常响应不能显示成功', async () => {
  for (const broken of [ { ...batchResponse(9), created_count: 9 },
                        { created_count: 10, medicines: Array.from({length: 10}, () => ({ ...fixture })) } ]) {
    let state; const original = api.request; api.request = async () => broken;
    try {
      state = await batchContext(); batchValues(state); state.form.requestSubmit(); state.form.requestSubmit();
      await settled(() => [...state.modal.querySelectorAll('button')].some(b => b.textContent === '刷新核实登记结果' && !b.hidden));
      assert(state.ctx.toasts.length === 0); assert(state.ctx.navigations.length === 0);
    } finally { api.request = original; batchCleanup(state); }
  }
});
await test('批量登记：确认摘要中的HTML字符保持文本', async () => {
  let state;
  try {
    state = await batchContext(); batchValues(state); state.name.value = '<svg onload=alert(1)>';
    state.form.requestSubmit(); assert(state.modal.textContent.includes('<svg onload=alert(1)>'));
    assert(state.modal.querySelector('svg') === null);
  } finally { batchCleanup(state); }
});

await runLogTests(test, assert);
await runUITests(test, assert);
await runLogExportTests(test, assert);

document.getElementById('summary').textContent = `${passed} 通过 / ${failed} 失败`;
document.title = failed ? `FAIL ${failed}` : `PASS ${passed}`;
