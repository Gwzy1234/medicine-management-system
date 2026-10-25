import { renderOperationLogs } from './logs.js';
import { api, ApiError, query, records, medicineRecord, scanRecord, stockRecord, userRecord } from './api.js';
import { roles } from './auth.js';
import { el, icon, button, field, textInput, select, heading, panel, empty, table, badge, states,
  uuid, details, medicineDetails, roleBadge, dialog, alertBox, expired } from './ui.js';

const categoryOptions = [['all', '全部状态'], ['in_stock', '在库'], ['dispensed', '已领取'],
  ['expired', '已过期'], ['expiring', '即将到期'], ['disabled', '已停用']];
const medicines = async (ctx, parameters = {}) => records(await ctx.request(`/api/medicines${query(parameters)}`), medicineRecord);
const stocks = async ctx => records(await ctx.request('/api/inventory'), stockRecord);

function exportButton(ctx) {
  if (ctx.user().role !== 'admin') return null;
  const target = button('导出 CSV', async () => {
    const result = await ctx.run(target, async () => {
      const file = await api.request('/api/export/medicines', { file: true });
      if (!ctx.current()) return;
      const url = URL.createObjectURL(file.blob);
      const link = el('a', { href: url, download: file.filename });
      try { document.body.append(link); link.click(); }
      finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    }, { permit: ['admin'] });
    if (result.ok && ctx.current()) ctx.toast('CSV 已交给浏览器下载，包含全部已登记记录。');
  }, '', 'download');
  return target;
}

function refresh(ctx) { return button('刷新', ctx.reload, '', 'refresh'); }
function countNote(count) { return el('div', { class: 'count-note', text: `共 ${count} 条记录 · 每条对应一支实体药品` }); }

export async function renderDashboard(ctx) {
  const snapshot = await ctx.request('/api/dashboard');
  const inventory = records(snapshot?.inventory, stockRecord);
  const all = records(snapshot?.medicines, row => ({ ...medicineRecord(row), business_date: snapshot.business_date }));
  if (!/^\d{4}-\d{2}-\d{2}$/.test(snapshot.business_date) ||
      !Number.isSafeInteger(snapshot.registered_count) || snapshot.registered_count !== all.length ||
      !Number.isSafeInteger(snapshot.dispensed_count) || snapshot.dispensed_count < 0 ||
      snapshot.dispensed_count > snapshot.registered_count) throw new ApiError('总览响应格式异常。');
  if (!ctx.current()) return;
  const summaries = [
    ['账面库存总数', inventory.reduce((sum, row) => sum + row.stock, 0), '支', '包含过期及停用的在库药品', 'inventory'],
    ['当前可领取数量', inventory.reduce((sum, row) => sum + row.available_stock, 0), '支', '在库、启用，且尚未过期', 'shield'],
    ['已领取药品', snapshot.dispensed_count, '支', '每次领取一支，记录持续保留', 'check'],
    ['已登记药品种类', inventory.length, '种', '同种药品按统一名称统计', 'medicine'],
  ];
  const stats = el('div', { class: 'stats' }, summaries.map(([label, count, unit, note, symbol]) =>
    el('section', { class: 'stat' }, el('div', { class: 'stat-top' }, label, el('span', { class: 'stat-icon' }, icon(symbol))),
      el('div', { class: 'stat-number' }, String(count), el('small', { text: unit })), el('div', { class: 'stat-note', text: note }))));
  const stockPanel = panel('药品库存速览', '账面库存与可领取数量，分别统计', button('查看全部', () => ctx.navigate('inventory'), 'text small', 'arrow'));
  stockPanel.body.className = '';
  if (inventory.length) stockPanel.body.append(...inventory.slice(0, 5).map(row =>
    el('div', { class: 'stock-line' }, el('div', {}, el('strong', { text: row.name }), el('small', { text: `账面 ${row.stock} 支 · 可领取 ${row.available_stock} 支` })),
      el('progress', { max: Math.max(1, row.stock), value: row.available_stock, 'aria-label': `${row.name}可领取比例` }),
      el('div', { class: 'count' }, String(row.available_stock), el('small', { text: '可领取 / 支' })))));
  else stockPanel.body.append(empty('还没有药品记录', ctx.user().role === 'admin' ? '从“药品管理”登记第一支药品。' : '管理员登记药品后，这里会显示实际库存。'));
  stockPanel.root.append(el('div', { class: 'info-line' }, icon('inventory'), '库存来自后端实时查询，过期与停用均不代表已领取。'));
  const promo = el('section', { class: 'panel panel-body nfc-promo' },
    el('div', {}, el('div', { class: 'eyebrow', text: 'NFC WORKFLOW' }), el('h2', {}, '识别一支药品，', el('br'), '确认每一次领取。'),
      el('p', { text: '模拟扫描或输入已有 UUID，识别后再由您确认。扫描本身不会改变库存。' }),
      button('进入 NFC 工作台', () => ctx.navigate('nfc'), 'primary', 'arrow')),
    el('div', { class: 'promo-art', 'aria-hidden': 'true' }, icon('nfc')));
  const recent = panel('最近登记的药品', '展示最近登记的 5 支药品', button('管理药品', () => ctx.navigate('medicines'), 'text small', 'arrow'));
  recent.root.classList.add('section-gap'); recent.body.className = '';
  recent.body.append(table([['药品名称', row => el('span', { class: 'main-text', text: row.name })],
    ['NFC UUID', row => uuid(row.nfc_uuid)], ['存放位置', row => row.location], ['有效期', row => row.expires_on], ['状态', states]], all.slice(-5).reverse()));
  ctx.root.replaceChildren(heading('科室管理总览', '掌握每一支药品的状态，让日常管理更清晰。', [exportButton(ctx), refresh(ctx)], 'OVERVIEW'),
    stats, el('div', { class: 'overview-grid' }, stockPanel.root, promo), recent.root);
}

function medicineForm(ctx, original = null) {
  const name = textInput('name', '例如：肾上腺素注射液', original?.name || ''); name.required = true;
  const location = textInput('location', '例如：CT 室急救柜 A 层', original?.location || '未指定'); location.required = true;
  const expiry = el('input', { name: 'expires_on', type: 'date', value: original?.expires_on || '', required: true, min: '0001-01-01', max: '9999-12-31' });
  const error = alertBox();
  const form = el('form', { class: 'form-stack' }, field('药品名称', name, '同种药品请使用统一名称，以便准确统计。'),
    field('存放位置', location), field('有效期', expiry), error);
  const submit = el('button', { type: 'submit', class: 'btn primary', text: original ? '保存资料' : '登记这支药品' });
  const cancel = button('取消', () => modal.close());
  form.append(el('div', { class: 'dialog-actions' }, cancel, submit));
  const modal = dialog(original ? '编辑药品资料' : '登记单支药品', el('div', {},
    el('p', { class: 'note', text: original ? '仅修改名称、位置和有效期。ID、UUID、领取与启用状态保持不变；当前不记录资料编辑日志。' : '每次登记一支药品。UUID 由后端自动生成，无需填写数量或标签编号。' }),
    original ? el('p', { class: 'note' }, 'NFC UUID：', uuid(original.nfc_uuid, true)) : null, form));
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (submit.disabled) return;
    if (!name.value.trim() || !location.value.trim()) { error.textContent = '药品名称和存放位置不能为空。'; return; }
    modal.lock(true); cancel.disabled = true;
    const result = await ctx.run(submit, async () => medicineRecord(await api.request(original ? `/api/medicines/${original.id}` : '/api/medicines',
      { method: original ? 'PATCH' : 'POST', body: { name: name.value, location: location.value, expires_on: expiry.value } })),
    { write: true, permit: ['admin'], errorTarget: error });
    modal.lock(false); cancel.disabled = false;
    if (result.ok && ctx.current()) { modal.close(); ctx.toast(original ? '药品资料已更新，库存将重新读取。' : '已登记一支药品，并生成唯一 UUID。'); ctx.reload(); }
  });
}

function batchMedicineForm(ctx) {
  const name = textInput('name', '例如：肾上腺素注射液'); name.required = true;
  const location = textInput('location', '例如：急救药品柜 A', '未指定'); location.required = true;
  const expiry = el('input', { name: 'expires_on', type: 'date', required: true, min: '0001-01-01', max: '9999-12-31' });
  const quantity = el('input', { name: 'quantity', type: 'number', min: 1, max: 100, step: 1, value: 1, required: true });
  const inputs = [name, location, expiry, quantity], error = alertBox(), summary = el('p', { class: 'note', hidden: true });
  let confirmed = null, working = false, lookupName = '';
  const submit = el('button', { type: 'submit', class: 'btn primary', text: '核对登记信息' });
  const cancel = button('取消', () => modal.close());
  const back = button('返回修改', edit); back.hidden = true;
  const lookup = button('刷新核实登记结果', () => {
    modal.close(); ctx.navigate('medicines', { name: lookupName, category: 'all' });
  }); lookup.hidden = true;
  const form = el('form', { class: 'form-stack batch-form' }, field('药品名称', name), field('存放位置', location),
    field('有效期', expiry), field('登记数量', quantity, '1～100 支，默认 1；每支均生成独立 ID 和 NFC UUID。'),
    summary, error, el('div', { class: 'dialog-actions' }, cancel, back, submit, lookup));
  const modal = dialog('批量登记药品', form);

  function edit() {
    confirmed = null; inputs.forEach(input => { input.disabled = false; });
    summary.hidden = true; back.hidden = true; submit.textContent = '核对登记信息';
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (working || submit.disabled) return;
    if (!confirmed) {
      const count = Number(quantity.value);
      if (!name.value.trim() || !location.value.trim() || !expiry.value || !Number.isInteger(count) || count < 1 || count > 100) {
        error.textContent = '请填写药品资料，登记数量须为 1～100 的整数。'; return;
      }
      confirmed = Object.freeze({ name: name.value.trim(), location: location.value.trim(), expires_on: expiry.value, quantity: count });
      summary.textContent = `即将登记${count}支${confirmed.name}，存放位置${confirmed.location}，有效期${confirmed.expires_on}。`;
      summary.hidden = false; inputs.forEach(input => { input.disabled = true; });
      submit.textContent = `确认登记 ${count} 支`; back.hidden = false; error.textContent = ''; return;
    }
    const attempt = confirmed;
    lookupName = attempt.name;
    working = true; modal.lock(true); cancel.disabled = true; back.disabled = true;
    const result = await ctx.run(submit, async () => {
      const value = await api.request('/api/medicines/batch', { method: 'POST', body: attempt });
      const rows = records(value?.medicines, medicineRecord);
      if (value.created_count !== attempt.quantity || rows.length !== attempt.quantity ||
          new Set(rows.map(row => row.id)).size !== rows.length || new Set(rows.map(row => row.nfc_uuid)).size !== rows.length ||
          rows.some(row => row.status !== 'in_stock' || row.is_active !== 1)) {
        throw new ApiError('批量登记响应尚未确认，请刷新核实，勿重复提交。', 0, 'contract_error');
      }
      return value;
    }, { write: true, permit: ['admin'], errorTarget: error });
    working = false; modal.lock(false); cancel.disabled = false; back.disabled = false;
    if (!ctx.current()) return;
    if (result.ok) {
      modal.close(); ctx.toast(`已成功登记 ${result.value.created_count} 支药品，每支均有独立 ID 和 UUID。`);
      // 列表按刚登记的名称重新读取；库存和总览进入页面时同样实时读取，不缓存旧统计。
      ctx.navigate('medicines', { name: result.value.medicines[0].name, category: 'all' });
    } else {
      error.textContent = result.error?.message || '本次未确认登记成功，请刷新核实。'; edit();
      if (!result.error || result.error.status === 0 || (result.error.status >= 500 && result.error.status !== 503)) {
        // 网络或响应异常可能发生在提交后；先查结果，不能盲目重发整批。
        error.textContent += ' 登记结果尚未确认，请先刷新核实，勿重复提交。';
        inputs.forEach(input => { input.disabled = true; });
        submit.disabled = true; lookup.hidden = false;
      }
    }
  });
}

function medicineActions(ctx, row) {
  const view = button('详情', async () => {
    const result = await ctx.run(view, async () => medicineRecord(await ctx.request(`/api/medicines/${row.id}`)));
    if (result.ok && ctx.current()) dialog('药品详情', medicineDetails(result.value));
  }, 'text small');
  const controls = [view];
  if (ctx.user().role === 'admin') {
    const edit = button('编辑', async () => {
      const result = await ctx.run(edit, async () => medicineRecord(await ctx.request(`/api/medicines/${row.id}`)), { permit: ['admin'] });
      if (result.ok && ctx.current()) {
        if (result.value.status !== 'in_stock') { ctx.toast('药品已领取，不能再编辑资料。', true); ctx.reload(); }
        else medicineForm(ctx, result.value);
      }
    }, 'text small');
    edit.disabled = row.status !== 'in_stock'; edit.title = edit.disabled ? '已领取药品不可编辑' : '编辑基本资料';
    const toggle = button(row.is_active ? '停用' : '启用', () => {
      const error = alertBox(), targetActive = row.is_active ? 'disable' : 'enable';
      const confirm = button(row.is_active ? '确认停用' : '确认启用', async () => {
        modal.lock(true); cancel.disabled = true;
        const result = await ctx.run(confirm, async () => {
          const value = await api.request(`/api/medicines/${row.id}/${targetActive}`, { method: 'POST' });
          medicineRecord(value?.medicine);
          if (typeof value.changed !== 'boolean') throw new ApiError('状态更新响应异常，请刷新核实。');
          return value;
        }, { write: true, permit: ['admin'], errorTarget: error });
        modal.lock(false); cancel.disabled = false;
        if (result.ok && ctx.current()) { modal.close(); ctx.toast(result.value.changed ? '药品启用状态已更新。' : '药品已是该状态，未重复写入日志。'); ctx.reload(); }
      }, row.is_active ? 'danger' : 'primary');
      const cancel = button('取消', () => modal.close());
      const modal = dialog(row.is_active ? '停用药品' : '启用药品', el('div', {}, medicineDetails(row),
        el('p', { class: 'note', text: '此操作只改变启用状态，不删除药品、不改变领取状态。已领取药品启用后也不能再次领取。' }),
        error, el('div', { class: 'dialog-actions' }, cancel, confirm)));
    }, 'text small');
    controls.push(edit, toggle);
  }
  return el('div', { class: 'actions' }, controls);
}

export async function renderMedicines(ctx) {
  const parameters = { name: ctx.parameters.name || '', category: ctx.parameters.category || 'all', days: ctx.parameters.days ?? 30 };
  const rows = await medicines(ctx, parameters);
  if (!ctx.current()) return;
  const name = textInput('name', '输入完整药品名称', parameters.name);
  const category = select('category', categoryOptions, parameters.category);
  const days = el('input', { type: 'number', name: 'days', min: 0, max: 3650, value: parameters.days });
  const daysField = field('临期天数', days); daysField.classList.add('compact');
  const syncDays = () => { daysField.hidden = category.value !== 'expiring'; days.disabled = daysField.hidden; };
  syncDays(); category.addEventListener('change', syncDays);
  const search = el('button', { class: 'btn primary', type: 'submit', text: '查询药品' });
  const filters = el('form', { class: 'filter-bar' }, field('药品名称 · 精确查询', name), field('状态筛选', category), daysField,
    el('div', { class: 'button-row' }, search, button('重置', () => ctx.navigate('medicines'))));
  filters.addEventListener('submit', event => {
    event.preventDefault();
    ctx.navigate('medicines', { name: name.value.trim(), category: category.value,
      ...(days.disabled ? {} : { days: Number(days.value || 0) }) });
  });
  const add = ctx.user().role === 'admin' ? button('登记药品', () => medicineForm(ctx), 'primary', 'plus') : null;
  const batch = ctx.user().role === 'admin' ? button('批量登记药品', () => batchMedicineForm(ctx), '', 'plus') : null;
  const listing = el('section', { class: 'panel' }, filters,
    table([['药品 ID', row => `#${row.id}`], ['药品名称', row => el('span', { class: 'main-text', text: row.name })], ['NFC UUID', row => uuid(row.nfc_uuid)],
      ['存放位置', row => row.location], ['有效期', row => el('div', {}, row.expires_on, expired(row) ? el('div', {}, badge('已过期', 'red')) : null)],
      ['领取状态', row => badge(row.status === 'in_stock' ? '在库' : '已领取', row.status === 'in_stock' ? 'blue' : '')],
      ['启用状态', row => badge(row.is_active ? '启用' : '停用', row.is_active ? 'green' : '')], ['操作', row => medicineActions(ctx, row)]], rows), countNote(rows.length));
  ctx.root.replaceChildren(heading('药品管理', '逐支管理药品资料与 UUID；停用会保留记录和历史。', [exportButton(ctx), add, batch, refresh(ctx)], 'MEDICINES'), listing);
}

export async function renderInventory(ctx) {
  const searched = ctx.parameters.name || '';
  const rows = searched ? [stockRecord(await ctx.request(`/api/inventory/by-name${query({ name: searched })}`))] : await stocks(ctx);
  if (!ctx.current()) return;
  const name = textInput('name', '输入完整药品名称', searched);
  const filters = el('form', { class: 'filter-bar' }, field('药品名称 · 精确查询', name), el('div', { class: 'button-row' },
    el('button', { class: 'btn primary', type: 'submit', text: '查询库存' }), button('查看全部', () => ctx.navigate('inventory'))));
  filters.addEventListener('submit', event => { event.preventDefault(); ctx.navigate('inventory', { name: name.value.trim() }); });
  ctx.root.replaceChildren(heading('库存管理', '账面库存包含过期、停用的在库药品；可领取库存额外要求启用且未过期。', [exportButton(ctx), refresh(ctx)], 'INVENTORY'),
    el('section', { class: 'panel' }, filters, table([['药品名称', row => el('span', { class: 'main-text', text: row.name })],
      ['账面库存 / 支', row => String(row.stock)], ['可领取库存 / 支', row => el('strong', { text: row.available_stock })],
      ['操作', row => button('查看单支记录', () => ctx.navigate('medicines', { name: row.name, category: 'all' }), 'text small', 'arrow')]], rows),
    el('div', { class: 'info-line' }, icon('inventory'), '每次操作成功后重新读取后端数据，不在网页中保存或扣减库存数字。')));
}

export async function renderNFC(ctx) {
  const inventory = await stocks(ctx);
  if (!ctx.current()) return;
  let selected = null, scanVersion = 0, working = false;
  const choice = select('medicine_name', [['', inventory.length ? '请选择药品名称' : '暂无已登记药品'], ...inventory.map(row => [row.name, `${row.name} · 可领取 ${row.available_stock} 支`])]);
  choice.required = true;
  const manual = textInput('nfc_uuid', '粘贴已有药品的完整 UUID'); manual.required = true; manual.autocomplete = 'off'; manual.spellcheck = false;
  const scanError = alertBox();
  const resultPanel = panel('识别结果', 'STEP 02 / 核对信息，确认后领取'); resultPanel.root.classList.add('scan-result');
  resultPanel.body.replaceChildren(empty('等待识别药品', '从左侧模拟扫描，或输入已有 UUID。扫描不会自动领取。'));
  const stage = el('div', { class: 'scan-stage' }, el('div', { class: 'step-label', text: 'STEP 01 / READ NFC UUID' }),
    el('div', { class: 'scan-device', 'aria-hidden': 'true' }, icon('nfc')), el('h2', { text: 'NFC 药品识别' }),
    el('p', { text: '读取数据库中的真实 UUID，不创建药品，不改变库存。' }));
  const simulate = el('button', { class: 'btn primary wide', type: 'submit', text: '模拟 NFC 扫描', disabled: inventory.length === 0 });
  const identify = el('button', { class: 'btn wide', type: 'submit', text: '识别 UUID' });
  const simulation = el('form', { class: 'form-stack' }, field('选择药品名称', choice), simulate,
    el('p', { class: 'scan-tabs-note', text: '后端将选择一支在库、启用且未过期的药品。' }));
  const manualForm = el('form', { class: 'form-stack' }, field('已有 NFC UUID', manual), identify);
  const manualBox = el('section', { class: 'panel panel-body manual-panel section-gap' }, el('h3', { text: '手动 UUID 识别' }),
    el('p', { text: '供后续标签联调使用。此入口不代表浏览器已具备 iPhone NFC 硬件读卡能力。' }), manualForm);

  function invalidate() {
    scanVersion += 1; selected = null;
    resultPanel.body.replaceChildren(empty('等待重新识别', '输入已变化，请重新扫描，避免领取另一支药品。'));
    scanError.textContent = '';
  }
  choice.addEventListener('change', invalidate); manual.addEventListener('input', invalidate);

  async function updateChoices() {
    const previous = choice.value, latest = await stocks(ctx);
    if (!ctx.current()) return;
    choice.replaceChildren(el('option', { value: '', text: latest.length ? '请选择药品名称' : '暂无已登记药品' }),
      ...latest.map(row => el('option', { value: row.name, text: `${row.name} · 可领取 ${row.available_stock} 支` })));
    choice.value = latest.some(row => row.name === previous) ? previous : '';
    simulate.disabled = latest.length === 0;
  }

  function renderResult(row, outcome = 'ready') {
    // 领取资格来自服务器扫描结果；客户端时钟只可用于一般日期展示。
    const available = row.can_dispense === true;
    const success = outcome === 'success';
    const content = el('div', {}, badge(success ? '领取成功' : '识别成功', success ? 'green' : 'blue'),
      el('div', { class: 'result-name', text: row.name }),
      el('p', { class: 'scan-summary', text: success ? '已领取一支药品。状态与领取日志已由后端事务提交。' : '请核对以下信息；识别结果不会预留这支药品。' }), medicineDetails(row));
    const footer = el('div', { class: 'section-gap' });
    if (!success && available && ctx.user().role !== 'viewer') {
      footer.append(button('确认领取', () => confirmDispense(row), 'primary wide', 'check'),
        el('small', { text: '每次仅领取一支。提交时会再次核验权限和药品状态。' }));
    } else if (!success) footer.append(alertBox(ctx.user().role === 'viewer' ? '当前为只读账号，可以识别药品，不能领取。' : '此药品当前不可领取。请检查领取、启用和过期状态。', ''));
    if (success) footer.append(alertBox('本 UUID 已完成领取，不能再次领取。', 'success'));
    footer.append(button('重新扫描 / 刷新', ctx.reload, 'quiet wide', 'refresh'));
    content.append(footer); resultPanel.body.replaceChildren(content);
  }

  function confirmDispense(row) {
    if (working || !selected || selected.nfc_uuid !== row.nfc_uuid) return;
    const captured = Object.freeze({ ...row });
    const error = alertBox();
    const cancel = button('取消领取', () => modal.close());
    const submit = button('确认领取 1 支', async () => {
      if (working || !selected || selected.nfc_uuid !== captured.nfc_uuid) return;
      working = true; modal.lock(true); cancel.disabled = true;
      const response = await ctx.run(submit, async () => {
        const value = await api.request('/api/nfc/dispense', { method: 'POST', body: {
          nfc_uuid: captured.nfc_uuid, name: captured.name, location: captured.location, expires_on: captured.expires_on,
        } });
        medicineRecord(value?.medicine);
        if (value.success !== true || value.reason !== 'dispensed' || value.medicine.status !== 'dispensed' || value.medicine.nfc_uuid !== captured.nfc_uuid) {
          throw new ApiError('领取响应未确认成功，请刷新核实，勿重复提交。', 0, 'contract_error');
        }
        return value.medicine;
      }, { write: true, permit: ['admin', 'staff'], errorTarget: error });
      working = false; modal.lock(false); cancel.disabled = false;
      if (!ctx.current()) return;
      if (response.ok) {
        modal.close(); selected = response.value; renderResult(selected, 'success');
        ctx.toast('领取成功：1 支。');
        try {
          await updateChoices();
          const stock = stockRecord(await ctx.request(`/api/inventory/by-name${query({ name: selected.name })}`));
          if (ctx.current()) resultPanel.body.append(el('p', { class: 'result-stock', text: `最新库存：账面 ${stock.stock} 支，可领取 ${stock.available_stock} 支。` }));
        } catch (failure) { if (ctx.current()) scanError.textContent = `领取已经成功，但库存刷新失败。${failure.message}`; }
      } else if (response.error) {
        modal.close(); selected = null;
        scanError.textContent = response.error.status === 409 ? `药品状态已变化：${response.error.message} 请重新扫描或刷新。` : response.error.message;
        resultPanel.body.replaceChildren(empty('本次未确认领取成功', '请重新扫描核实最新状态；不要直接重复提交。'), button('重新扫描 / 刷新', ctx.reload, 'wide', 'refresh'));
      }
    }, 'primary');
    const modal = dialog('确认领取药品', el('div', {},
      el('p', { class: 'note', text: '请确认药品与 UUID。这一步将实际领取一支药品，并写入操作日志。' }),
      medicineDetails(captured), error, el('div', { class: 'dialog-actions' }, cancel, submit)));
  }

  async function scan(kind, target) {
    if (working) return;
    working = true; selected = null; const version = ++scanVersion;
    const payload = kind === 'simulate' ? { name: choice.value } : { nfc_uuid: manual.value.trim() };
    const other = target === simulate ? identify : simulate;
    other.disabled = true; stage.classList.add('scanning'); stage.setAttribute('aria-busy', 'true');
    resultPanel.body.replaceChildren(el('div', { class: 'loading', role: 'status', text: '正在识别药品…' }));
    const response = await ctx.run(target, async () => scanRecord(await ctx.request(`/api/nfc/${kind}`, { method: 'POST', body: payload })), { errorTarget: scanError });
    working = false; stage.classList.remove('scanning'); stage.setAttribute('aria-busy', 'false');
    identify.disabled = false; simulate.disabled = choice.options.length <= 1;
    if (!ctx.current() || version !== scanVersion) return;
    if (response.ok) { selected = response.value; renderResult(selected); }
    else resultPanel.body.replaceChildren(empty('未识别到可用结果', '检查名称或 UUID，并查看上方错误提示。扫描未改变库存。'));
  }
  simulation.addEventListener('submit', event => { event.preventDefault(); scan('simulate', simulate); });
  manualForm.addEventListener('submit', event => { event.preventDefault(); scan('scan', identify); });
  ctx.root.replaceChildren(heading('NFC 识别与领取', '先识别，再核对，最后确认领取。模拟与手动识别共用同一个 UUID 领取入口。', [refresh(ctx)], 'NFC WORKSPACE'),
    scanError, el('div', { class: 'scan-grid section-gap' }, el('div', {}, el('section', { class: 'panel' }, stage, el('div', { class: 'panel-body' }, simulation)), manualBox), resultPanel.root));
}

export async function renderLogs(ctx) {
  return renderOperationLogs(ctx);
}

function createUserForm(ctx) {
  const name = textInput('username', '输入用户名'); name.required = true; name.autocomplete = 'off';
  const password = el('input', { name: 'password', type: 'password', autocomplete: 'new-password', required: true, minLength: 8, maxLength: 2048, placeholder: '8～1024 个字符' });
  const role = select('role', Object.entries(roles).map(([key, title]) => [key, `${title} (${key})`]), 'staff');
  const error = alertBox();
  const form = el('form', { class: 'form-stack' }, field('用户名', name, '去除首尾空白后 1～500 个字符。'), field('初始密码', password), field('用户角色', role), error);
  const cancel = button('取消', () => modal.close()), submit = el('button', { class: 'btn primary', type: 'submit', text: '创建用户' });
  form.append(el('div', { class: 'dialog-actions' }, cancel, submit));
  const modal = dialog('创建用户', el('div', {}, el('p', { class: 'note', text: '管理员可管理药品与用户；工作人员可查询和领取；只读用户只能查看药品信息。' }), form));
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (submit.disabled) return;
    modal.lock(true); cancel.disabled = true;
    const result = await ctx.run(submit, async () => userRecord(await api.request('/api/users', { method: 'POST', body: { username: name.value, password: password.value, role: role.value } })),
      { write: true, permit: ['admin'], errorTarget: error });
    password.value = ''; modal.lock(false); cancel.disabled = false;
    if (result.ok && ctx.current()) { modal.close(); ctx.toast('用户已创建。'); ctx.reload(); }
  });
}

export async function renderUsers(ctx) {
  const rows = records(await ctx.request('/api/users'), userRecord);
  if (!ctx.current()) return;
  ctx.root.replaceChildren(heading('用户管理', '管理科室访问账号。权限最终由 FastAPI 与数据库业务层校验。',
    [button('创建用户', () => createUserForm(ctx), 'primary', 'plus'), refresh(ctx)], 'ACCESS MANAGEMENT'),
    el('section', { class: 'panel' }, table([['用户 ID', row => `#${row.id}`], ['用户名', row => el('span', { class: 'main-text', text: row.username })],
      ['角色', row => roleBadge(row.role)], ['操作', row => {
        const target = button('查看详情', async () => {
          const response = await ctx.run(target, async () => userRecord(await ctx.request(`/api/users/${row.id}`)), { permit: ['admin'] });
          if (response.ok && ctx.current()) dialog('用户详情', details([['用户 ID', String(response.value.id)], ['用户名', response.value.username], ['角色', roleBadge(response.value.role)]]));
        }, 'text small');
        return target;
      }]], rows), el('div', { class: 'info-line' }, icon('shield'), '此阶段支持创建与查看账号，不提供用户删除、密码修改或角色变更。')));
}
