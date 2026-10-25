// 真实浏览器 DOM 回归：只向视图提供虚构日志，不执行任何数据库写操作。
import { renderOperationLogs, groupSimilarLogs } from '/static/js/logs.js';
import { ApiError } from '/static/js/api.js';

export async function runLogTests(test, assert) {
  const row = (id = 1, extra = {}) => ({ id, user_id: 1, username: '虚构管理员', medicine_id: id,
    nfc_uuid: `00000000-0000-4000-8000-${String(id).padStart(12, '0')}`, medicine_name: '肾上腺素注射液',
    action: 'register', quantity: 1, created_at: '2026-10-23 11:35:35', ...extra });
  const batch = Array.from({ length: 25 }, (_, index) => row(index + 1));
  const sample = [row(1), row(2, { action: 'dispense', user_id: 2, username: '虚构工作人员', created_at: '2026-10-23 11:36:00' }),
    row(3, { medicine_name: '阿托品', action: 'disable', quantity: 2, created_at: '2026-10-22 11:35:35' })];
  const ids = root => [...root.querySelectorAll('[data-log-id]')].map(node => Number(node.dataset.logId));
  const change = (control, value, event = 'change') => { control.value = value; control.dispatchEvent(new Event(event, { bubbles: true })); };
  const click = (root, text) => [...root.querySelectorAll('button')].find(node => node.textContent === text).click();
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  async function withPage(rows, action, options = {}) {
    const root = document.createElement('div'); document.body.append(root);
    const calls = [], navigations = []; let reloads = 0;
    const ctx = { root, parameters: options.parameters || {}, user: () => options.user || { id: 1, username: '虚构管理员', role: 'admin' },
      request: async path => { calls.push(path); if (options.error) throw options.error; return rows; },
      current: () => true, navigate: (...args) => navigations.push(args), reload: () => reloads++, ...options.ctx };
    try { await renderOperationLogs(ctx); await action({ root, ctx, calls, navigations, reloads: () => reloads }); }
    finally { root.remove(); }
  }
  await test('日志：单条记录突出药名，保留数量、操作人和UTC语义', () => withPage([row(1)], ({ root }) => {
    assert(root.querySelector('h3').textContent === '肾上腺素注射液');
    assert(root.querySelector('.activity-tag').textContent === '登记药品');
    assert(root.textContent.includes('虚构管理员')); assert(root.textContent.includes('1 支'));
    assert(root.querySelector('time').dateTime === '2026-10-23T11:35:35Z');
    assert(root.querySelector('time').title.includes('11:35:35')); assert(root.textContent.includes('上海时间'));
  }));
  await test('日志：25条相似登记折叠一组，展开收起始终保留25条原始记录', () => withPage(batch, async ({ root }) => {
    const group = root.querySelector('.activity-group'); assert(group && !group.open);
    assert(root.querySelectorAll('.activity-group').length === 1); assert(new Set(ids(root)).size === 25);
    assert(root.textContent.includes('相似记录分组 · 非实际批次')); assert(root.textContent.includes('登记记录 25 条'));
    group.querySelector('summary').click(); await tick(); assert(group.open); assert(ids(root).length === 25);
    group.querySelector('summary').click(); await tick(); assert(!group.open); assert(ids(root).length === 25);
  }));
  await test('日志：分组不跨操作类型、药名、用户、原始时间或相邻边界', () => {
    const sequence = [row(1), row(2), row(3, { action: 'dispense' }), row(4), row(5, { user_id: 2 }),
      row(6, { medicine_name: '阿托品' }), row(7, { created_at: '2026-10-23 11:35:36' }), row(8, { action: 'enable' }), row(9, { action: 'enable' })];
    const original = JSON.stringify(sequence), groups = groupSimilarLogs(sequence);
    assert(groups.length === 8); assert(groups[0].rows.length === 2);
    assert(groups.flatMap(group => group.rows).every((item, index) => item === sequence[index]));
    assert(JSON.stringify(sequence) === original);
  });
  await test('日志：关闭相似折叠可独立查看全部记录', () => withPage(batch, ({ root }) => {
    const checkbox = root.querySelector('.activity-grouping input'); checkbox.checked = false; checkbox.dispatchEvent(new Event('change'));
    assert(!root.querySelector('.activity-group')); assert(ids(root).length === 25);
  }));
  await test('日志：中文部分名称搜索与类型筛选组合且不重复显示', () => withPage(sample, ({ root }) => {
    change(root.querySelector('[name=log_name]'), '上腺', 'input'); assert(ids(root).length === 2);
    change(root.querySelector('[name=log_action]'), 'dispense'); assert(ids(root).join() === '2');
    assert(root.textContent.includes('显示 1 条原始日志 / 已加载 3 条'));
  }));
  await test('日志：领取快捷筛选可迅速定位领取记录', () => withPage(sample, ({ root }) => {
    click(root, '领取记录 1'); assert(ids(root).join() === '2');
    assert(root.querySelector('.activity-chip-dispense').getAttribute('aria-pressed') === 'true');
  }));
  await test('日志：管理员操作人筛选仅列出已加载操作人', () => withPage(sample, ({ root }) => {
    const select = root.querySelector('[name=log_actor]'); assert(select.options.length === 3);
    change(select, '2'); assert(ids(root).join() === '2');
    assert(root.textContent.includes('仅作用于本次已加载记录'));
  }));
  await test('日志：时间正反排序及同秒ID次序确定，原始数据不变', () => {
    const source = [row(1), row(2), row(3, { created_at: '2026-10-24 00:00:00' })], before = JSON.stringify(source);
    return withPage(source, ({ root }) => {
      assert(root.querySelector('[name=log_order]').value === 'newest');
      assert(ids(root).join() === '3,2,1'); change(root.querySelector('[name=log_order]'), 'oldest');
      assert(ids(root).join() === '1,2,3'); assert(JSON.stringify(source) === before);
    });
  });
  await test('日志：清除本地条件恢复全部已加载记录', () => withPage(sample, ({ root }) => {
    change(root.querySelector('[name=log_name]'), '不存在', 'input'); assert(ids(root).length === 0);
    click(root, '清除筛选'); assert(ids(root).length === 3); assert(root.querySelector('[name=log_order]').value === 'newest');
  }));
  await test('日志：服务器参数保持原契约，清除读取范围重新请求权限内记录', () => withPage(sample, ({ root, calls, navigations }) => {
    assert(calls[0] === '/api/logs?user_id=2&medicine_id=1&action=dispense');
    assert(root.textContent.includes('当前服务器读取范围')); click(root, '清除筛选');
    assert(navigations.length === 1 && navigations[0][0] === 'logs' && navigations[0].length === 1);
  }, { parameters: { user_id: '2', medicine_id: '1', action: 'dispense', name: '不能传给API', sort: 'oldest' } }));
  await test('日志：编号查询继续验证并保留SQLite64位ID精度', () => withPage(sample, ({ root, navigations }) => {
    const input = root.querySelector('[name=medicine_id]'), form = root.querySelector('.activity-range-form');
    input.value = '9223372036854775808'; form.requestSubmit(); assert(navigations.length === 0);
    input.value = '9223372036854775807'; form.requestSubmit();
    assert(navigations[0][1].medicine_id === '9223372036854775807');
  }));
  await test('日志：staff仅本人，无其他操作人选项，不提交其他用户ID', () => withPage([sample[1]], ({ root, calls }) => {
    assert(!root.querySelector('[name=log_actor]')); assert(!root.querySelector('[name=user_id]'));
    assert(root.textContent.includes('仅本人记录')); assert(!calls[0].includes('user_id'));
  }, { user: { id: 2, username: '虚构工作人员', role: 'staff' }, parameters: { user_id: 1 } }));
  await test('日志：viewer拒绝且不发送日志请求', () => withPage(sample, ({ root, calls }) => {
    assert(calls.length === 0); assert(root.textContent.includes('无权')); assert(ids(root).length === 0);
  }, { user: { id: 3, username: '虚构查看者', role: 'viewer' } }));
  await test('日志：staff收到外部用户记录时拒绝展示', () => withPage(sample, ({ root }) => {
    assert(root.textContent.includes('权限范围异常')); assert(ids(root).length === 0);
  }, { user: { id: 2, username: '虚构工作人员', role: 'staff' } }));
  await test('日志：UUID默认缩略，展开完整值并能选中', () => withPage([row()], ({ root }) => {
    assert(root.querySelector('code').textContent.includes('…'));
    const details = root.querySelector('.activity-uuid-full'); details.querySelector('summary').click(); assert(details.open);
    const input = details.querySelector('input'); assert(input.value === row().nfc_uuid && input.readOnly);
    click(root, '选中完整UUID'); assert(input.selectionEnd - input.selectionStart === row().nfc_uuid.length);
  }));
  for (const failure of [false, true]) {
    await test(`日志：UUID${failure ? '复制失败提供手动回退，不误报成功' : '复制完整值而非缩略值'}`, async () => {
      const descriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard'); let copied;
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => {
        if (failure) throw new Error('测试拒绝'); copied = value;
      } } });
      try { await withPage([row()], async ({ root }) => {
        click(root, '复制UUID'); await tick(); const status = root.querySelector('.activity-copy-status').textContent;
        if (failure) { assert(root.querySelector('.activity-uuid-full').open); assert(status.includes('手动复制')); assert(!status.includes('已复制')); }
        else { assert(copied === row().nfc_uuid); assert(status === '已复制完整UUID'); }
      }); } finally { if (descriptor) Object.defineProperty(navigator, 'clipboard', descriptor); else delete navigator.clipboard; }
    });
  }
  await test('日志：中文及HTML特殊字符均作为文本，不创建注入节点', () => {
    const payload = '<img src=x onerror=alert(1)> & 中文';
    return withPage([row(1, { medicine_name: payload, username: payload, nfc_uuid: payload })], ({ root }) => {
      assert(root.querySelector('h3').textContent === payload); assert(root.querySelector('.activity-meta strong').textContent === payload);
      assert(root.querySelector('.activity-uuid-full input').value === payload); assert(!root.querySelector('img'));
    });
  });
  await test('日志：启停数量原样显示且不表示库存变化', () => withPage([sample[2]], ({ root }) => {
    assert(root.querySelector('.activity-quantity strong').textContent === '2 支'); assert(root.textContent.includes('不代表库存增减'));
  }));
  await test('日志：空记录正常显示，条件无匹配与空库文案区分', () => withPage([], ({ root }) => {
    assert(root.textContent.includes('暂无操作日志')); assert(root.textContent.includes('显示 0 条原始日志 / 已加载 0 条'));
  }));
  await test('日志：API失败显示错误和重读入口，不伪装成空日志', () => withPage([], ({ root, reloads }) => {
    assert(root.textContent.includes('日志读取失败：临时测试503')); assert(!root.textContent.includes('暂无操作日志'));
    click(root, '重新读取日志'); assert(reloads() === 1);
  }, { error: new ApiError('临时测试503', 503, 'unavailable') }));
  await test('日志：账号已切换时延迟错误不覆盖新页面', async () => {
    const root = document.createElement('div'); let current = true, reject;
    const pending = renderOperationLogs({ root, user: () => ({ id: 1, role: 'admin' }), parameters: {}, current: () => current,
      reload() {}, request: () => new Promise((resolve, fail) => { reject = fail; }) });
    current = false; root.textContent = '新账号页面'; reject(new ApiError('旧账号错误', 401)); await pending;
    assert(root.textContent === '新账号页面');
  });
  await test('日志：重复日志ID响应被拒绝，避免错误分组与计数', () => withPage([row(), row()], ({ root }) => {
    assert(root.textContent.includes('日志数据或权限范围异常')); assert(ids(root).length === 0);
  }));
}
