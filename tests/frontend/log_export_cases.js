import { renderOperationLogs } from '/static/js/logs.js';
import { createClient, ApiError } from '/static/js/api.js';

export async function runLogExportTests(test, assert) {
  const filename = '操作日志_2026-10-24_20-30-45.csv';
  const disposition = `attachment; filename*=utf-8''${encodeURIComponent(filename)}`;
  const file = () => ({ blob: new Blob(['\ufeff日志ID,操作类型\r\n1,register\r\n'], { type: 'text/csv' }), filename });
  const rows = Array.from({ length: 25 }, (_, index) => ({ id: index + 1, user_id: 1, username: '虚构管理员', medicine_id: index + 1,
    nfc_uuid: `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`, medicine_name: '虚构批量药品',
    action: 'register', quantity: 1, created_at: '2026-10-24 12:30:00' }));
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  async function page(action, options = {}) {
    const root = document.createElement('main'); document.body.append(root);
    const calls = [], toasts = [], downloads = [], created = [], revoked = [];
    let current = true;
    const originalCreate = URL.createObjectURL, originalRevoke = URL.revokeObjectURL, originalClick = HTMLAnchorElement.prototype.click;
    URL.createObjectURL = blob => { assert(blob instanceof Blob); const value = `blob:fictional-${created.length}`; created.push(value); return value; };
    URL.revokeObjectURL = value => revoked.push(value);
    HTMLAnchorElement.prototype.click = function () { downloads.push({ filename: this.download, href: this.href }); };
    const ctx = { root, parameters: options.parameters || {}, user: () => options.user || { id: 1, username: '虚构管理员', role: 'admin' },
      current: () => current, reload() {}, navigate() {}, toast: message => toasts.push(message),
      request: async (path, settings) => {
        calls.push({ path, settings });
        if (path === '/api/export/logs') return options.exporter ? options.exporter() : file();
        if (options.readError) throw new ApiError('读取列表暂时失败', 503);
        return options.rows || rows;
      },
      async run(target, work, settings) {
        if (target.disabled) return { ok: false };
        target.disabled = true; const text = target.textContent; target.textContent = '正在处理…';
        try { assert(settings.permit.join() === 'admin,staff'); return { ok: true, value: await work() }; }
        catch (error) { settings.errorTarget.textContent = error.message; return { ok: false, error }; }
        finally { target.disabled = false; target.textContent = text; }
      } };
    try {
      await renderOperationLogs(ctx);
      const button = [...root.querySelectorAll('button')].find(n => n.textContent === '导出日志 CSV');
      await action({ root, button, calls, toasts, downloads, created, revoked, leave: () => { current = false; } });
    } finally {
      if (created.length) await new Promise(resolve => setTimeout(resolve, 1050));
      URL.createObjectURL = originalCreate; URL.revokeObjectURL = originalRevoke; HTMLAnchorElement.prototype.click = originalClick; root.remove();
    }
  }
  await test('日志导出：解析服务器UTF-8附件名，BOM保留，JWT只在请求头', async () => {
    let seen;
    const client = createClient({ fetcher: async (path, options) => { seen = { path, options };
      return new Response(new Uint8Array([239, 187, 191, 105, 100, 10]), { headers: { 'Content-Type': 'text/csv', 'Content-Disposition': disposition } }); } });
    client.setToken('fictional-unit-token'); const result = await client.request('/api/export/logs', { file: true });
    assert(result.filename === filename); assert(new Uint8Array(await result.blob.arrayBuffer())[0] === 239);
    assert(seen.path === '/api/export/logs'); assert(seen.options.headers.Authorization === 'Bearer fictional-unit-token');
    assert(!seen.path.includes('token'));
  });
  await test('日志导出：拒绝服务器异常文件名及任意路径', async () => {
    for (const value of ['', "attachment; filename*=utf-8''..%2Fsecret.csv", "attachment; filename*=utf-8''%ZZ", 'attachment; filename=other.csv']) {
      const client = createClient({ fetcher: async () => new Response('data', { headers: { 'Content-Type': 'text/csv', 'Content-Disposition': value } }) });
      let rejected = false;
      try { await client.request('/api/export/logs', { file: true }); } catch (error) { rejected = error.code === 'contract_error'; }
      assert(rejected);
    }
  });
  for (const role of ['admin', 'staff']) {
    await test(`日志导出：${role}按钮可见且明确全部权限范围`, () => page(({ root, button }) => {
      assert(button); assert(root.querySelector('.activity-export-scope').textContent.includes(role === 'admin' ? '有权查看的全部日志' : '本人全部日志'));
      assert(root.querySelector('.activity-export-scope').textContent.includes('不受页面筛选'));
    }, { user: { id: 1, username: '虚构操作者', role } }));
  }
  await test('日志导出：viewer没有导出按钮且不发请求', () => page(({ button, calls }) => {
    assert(!button && calls.length === 0);
  }, { user: { id: 3, username: '虚构查看者', role: 'viewer' } }));
  await test('日志导出：25条折叠及局部筛选不影响独立导出接口或列表', () => page(async ({ root, button, calls, downloads, toasts }) => {
    const list = root.querySelector('.activity-list'), originalCards = [...list.querySelectorAll('[data-log-id]')];
    assert(originalCards.length === 25); assert(!list.querySelector('.activity-group').open);
    const input = root.querySelector('[name=log_name]'); input.value = '无匹配'; input.dispatchEvent(new Event('input'));
    const emptyState = list.firstElementChild;
    button.click(); await tick();
    assert(calls.filter(r => r.path === '/api/export/logs').length === 1);
    const request = calls.find(r => r.path === '/api/export/logs'); assert(request.settings.file === true); assert(!request.settings.body);
    assert(downloads.length === 1 && downloads[0].filename === filename);
    assert(list.firstElementChild === emptyState); assert(toasts.length === 1);
  }));
  await test('日志导出：重复点击仅一次请求，显示等待并最终释放Blob URL', async () => {
    let resolve;
    await page(async ({ root, button, calls, created, revoked, downloads }) => {
      button.click(); button.click(); await tick();
      assert(button.disabled); assert(root.querySelector('.activity-export-scope').textContent.includes('正在生成'));
      assert(calls.filter(r => r.path === '/api/export/logs').length === 1);
      resolve(file()); await tick(); assert(!button.disabled && downloads.length === 1);
      assert(button.querySelector('svg'), '导出后恢复原工具栏图标');
      await new Promise(done => setTimeout(done, 1050)); assert(created.length === 1 && revoked.join() === created.join());
    }, { exporter: () => new Promise(done => { resolve = done; }) });
  });
  await test('日志导出：失败保留当前记录及分组，明确报错不误报成功', () => page(async ({ root, button, calls, downloads, toasts }) => {
    const group = root.querySelector('.activity-group'); group.open = true;
    button.click(); await tick();
    assert(root.querySelector('.activity-export-scope .alert').textContent.includes('虚构导出503'));
    assert(root.querySelector('.activity-group') === group && group.open);
    assert(root.querySelectorAll('[data-log-id]').length === 25);
    assert(toasts.length === 0 && downloads.length === 0 && !button.disabled);
    assert(calls.filter(r => r.path === '/api/export/logs').length === 1);
  }, { exporter: async () => { throw new ApiError('虚构导出503', 503); } }));
  await test('日志导出：页面或账号切换后延迟响应不能下载旧账号日志', async () => {
    let resolve;
    await page(async ({ button, leave, downloads, toasts, created }) => {
      button.click(); await tick(); leave(); resolve(file()); await tick();
      assert(downloads.length === 0 && toasts.length === 0 && created.length === 0);
    }, { exporter: () => new Promise(done => { resolve = done; }) });
  });
  await test('日志导出：空日志仍能下载表头CSV', () => page(async ({ root, button, downloads }) => {
    assert(root.textContent.includes('暂无操作日志')); button.click(); await tick(); assert(downloads.length === 1);
  }, { rows: [] }));
  await test('日志导出：原列表查询失败不伪造日志，独立导出仍可使用', () => page(async ({ root, button, calls, downloads }) => {
    assert(root.textContent.includes('日志读取失败')); button.click(); await tick();
    assert(calls.filter(r => r.path === '/api/export/logs').length === 1 && downloads.length === 1);
    assert(root.textContent.includes('日志读取失败'));
  }, { readError: true }));
}
