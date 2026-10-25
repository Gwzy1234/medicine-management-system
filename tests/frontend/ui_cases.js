// 只验证实际组件的浏览器布局；网络事务和权限仍由现有集成测试验证。
import { renderDashboard, renderMedicines, renderInventory, renderNFC, renderUsers } from '/static/js/views.js?v=macos-20261024';
import { renderOperationLogs } from '/static/js/logs.js';

export async function runUITests(test, assert) {
  const medicine = { id: 1, nfc_uuid: '11111111-2222-4333-8444-555555555555', name: '虚构肾上腺素注射剂',
    location: '虚构影像急救柜 A', expires_on: '2035-12-31', status: 'in_stock', is_active: 1, business_date: '2026-10-24' };
  const stock = { name: medicine.name, stock: 1, available_stock: 1 };
  const snapshot = { business_date: '2026-10-24', medicines: [medicine], inventory: [stock], registered_count: 1, dispensed_count: 0 };
  const user = { id: 1, username: '虚构UI测试管理员', role: 'admin' };
  const logs = Array.from({ length: 25 }, (_, index) => ({ id: index + 1, medicine_id: index + 1, nfc_uuid: medicine.nfc_uuid,
    medicine_name: medicine.name, user_id: 1, username: user.username, action: 'register', quantity: 1, created_at: '2026-10-24 00:00:00' }));
  async function frame(width, action) {
    const iframe = document.createElement('iframe'); iframe.title = `${width}px临时布局测试`;
    iframe.style.cssText = `position:absolute;left:-20000px;width:${width}px;height:900px;max-width:none;border:0;`;
    const loaded = new Promise((resolve, reject) => { iframe.onload = resolve; iframe.onerror = reject; });
    iframe.src = new URL('./ui_frame.html', import.meta.url).href; document.body.append(iframe);
    try {
      await loaded;
      const doc = iframe.contentDocument, win = iframe.contentWindow, root = doc.getElementById('fixture-root');
      const ctx = { root, parameters: {}, user: () => user, current: () => true, reload() {}, navigate() {}, toast() {},
        request: async path => path === '/api/dashboard' ? snapshot : path === '/api/medicines?category=all&days=30' ? [medicine] :
          path.startsWith('/api/medicines') ? [medicine] : path === '/api/inventory' ? [stock] : path === '/api/users' ? [user] : path === '/api/logs' ? logs : [] };
      await action({ doc, win, root, ctx });
    } finally { iframe.remove(); }
  }
  for (const width of [320, 390, 768, 1024, 1440]) {
    await test(`UI：${width}px下六个业务页面不出现整页横向溢出，控件保持触控尺寸`, () => frame(width, async ({ root, win, doc, ctx }) => {
      for (const [page, render] of [['dashboard', renderDashboard], ['medicines', renderMedicines], ['inventory', renderInventory],
                                   ['nfc', renderNFC], ['logs', renderOperationLogs], ['users', renderUsers]]) {
        root.dataset.page = page; await render(ctx);
        assert(doc.documentElement.scrollWidth <= doc.documentElement.clientWidth + 1, `${page}: ${doc.documentElement.scrollWidth} > ${width}`);
        for (const target of root.querySelectorAll('button,input:not([type=checkbox]),select')) {
          if (!target.getClientRects().length) continue;
          assert(target.getBoundingClientRect().height >= 43.5, `${page}: ${target.textContent || target.name}触控高度不足`);
        }
        assert(root.querySelector('h1')?.textContent, `${page}标题缺失`);
        if (page === 'logs') {
          const group = root.querySelector('.activity-group'); group.open = true;
          assert(group.querySelectorAll('[data-log-id]').length === 25);
          assert(doc.documentElement.scrollWidth <= width + 1, '展开日志后横向溢出');
        }
      }
    }));
  }
  await test('UI：长中文名称、用户名及UUID在手机记录卡中可安全换行', () => frame(320, async ({ root, doc, ctx }) => {
    const text = '虚构很长中文 & <img src=x onerror=alert(1)> '.repeat(10);
    ctx.request = async () => [{ ...logs[0], medicine_name: text, username: text, nfc_uuid: 'x'.repeat(500) }];
    await renderOperationLogs(ctx); root.querySelector('.activity-uuid-full').open = true;
    assert(doc.documentElement.scrollWidth <= 321); assert(!root.querySelector('img'));
    assert(root.querySelector('h3').textContent === text);
  }));
  await test('UI：关键文字、主按钮及四种状态标签对比度至少4.5比1', () => frame(768, async ({ root, win, doc, ctx }) => {
    function ratio(a, b) {
      const luma = value => {
        const numbers = value.match(/[\d.]+/g).slice(0, 3).map(n => Number(n) / 255).map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4);
        return numbers[0] * .2126 + numbers[1] * .7152 + numbers[2] * .0722;
      };
      const x = luma(a), y = luma(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
    }
    await renderMedicines(ctx);
    const controls = [root.querySelector('.btn.primary')];
    for (const color of ['blue', 'green', 'orange', 'red']) { const node = doc.createElement('span'); node.className = 'badge ' + color; node.textContent = '状态'; root.append(node); controls.push(node); }
    for (const node of controls) {
      const style = win.getComputedStyle(node); assert(ratio(style.color, style.backgroundColor) >= 4.5, `对比度不足: ${node.textContent}`);
    }
    const muted = doc.createElement('span'); muted.className = 'muted'; root.append(muted);
    assert(ratio(win.getComputedStyle(muted).color, 'rgb(245,245,247)') >= 4.5, '次级文字对比不足');
  }));
  await test('UI：低高度手机弹窗可滚动，输入字号避免小于16px', () => frame(320, async ({ root, doc, win, ctx }) => {
    await renderMedicines(ctx);
    const modal = doc.createElement('dialog'); modal.className = 'dialog';
    const content = doc.createElement('div'); content.className = 'dialog-body';
    for (let i = 0; i < 25; i++) { const input = doc.createElement('input'); input.type = 'text'; input.setAttribute('aria-label', `测试字段${i}`); content.append(input); }
    modal.append(content); doc.body.append(modal); modal.showModal();
    assert(modal.getBoundingClientRect().width <= 320); assert(modal.getBoundingClientRect().height < win.innerHeight);
    assert(modal.scrollHeight > modal.clientHeight); assert(win.getComputedStyle(modal).overflowY === 'auto');
    assert(parseFloat(win.getComputedStyle(modal.querySelector('input')).fontSize) >= 16); modal.close();
  }));
  await test('UI：NFC扫描环仅在真实scanning状态出现，静止状态无循环动画', () => frame(1024, async ({ root, win, ctx }) => {
    await renderNFC(ctx); const stage = root.querySelector('.scan-stage'), device = root.querySelector('.scan-device');
    assert(win.getComputedStyle(device, '::after').animationName === 'none');
    stage.classList.add('scanning');
    const name = win.getComputedStyle(device, '::after').animationName;
    assert(name === (win.matchMedia('(prefers-reduced-motion:reduce)').matches ? 'none' : 'pulse'));
    stage.classList.remove('scanning'); assert(win.getComputedStyle(device, '::after').animationName === 'none');
  }));
  await test('UI：总览数值仍来自单一快照，优先展示账面和可领取库存', () => frame(1440, async ({ root, ctx }) => {
    const paths = []; ctx.request = async path => { paths.push(path); return snapshot; }; await renderDashboard(ctx);
    assert(paths.join() === '/api/dashboard');
    const labels = [...root.querySelectorAll('.stat-top')].map(node => node.textContent);
    assert(labels[0] === '账面库存总数' && labels[1] === '当前可领取数量');
    assert(root.querySelectorAll('.stat').length === 4);
  }));
}
