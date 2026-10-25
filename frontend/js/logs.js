// 操作日志专用视图：只读展示，不修改日志；文本始终通过安全 DOM 工具插入。
import { ApiError, query, records } from './api.js';
import { el, icon, button, field, textInput, select, heading, loading, empty, alertBox, logTime, positiveFilter } from './ui.js';

const actions = {
  register: { label: '登记药品', color: 'register', icon: 'plus' },
  dispense: { label: '领取药品', color: 'dispense', icon: 'check' },
  disable: { label: '停用药品', color: 'disable', icon: 'shield' },
  enable: { label: '启用药品', color: 'enable', icon: 'refresh' },
};
const actionOptions = [['', '全部操作类型'], ...Object.entries(actions).map(([key, value]) => [key, value.label])];

function logRecord(row) {
  if (!row || ![row.id, row.user_id, row.medicine_id, row.quantity].every(v => Number.isSafeInteger(v) && v > 0) ||
      !['username', 'medicine_name', 'nfc_uuid', 'created_at'].every(key => typeof row[key] === 'string') ||
      !Object.hasOwn(actions, row.action)) throw new ApiError('日志响应格式异常，请重新读取。', 0, 'contract_error');
  return row;
}

function similarityKey(row) {
  return JSON.stringify([row.action, row.medicine_name, row.user_id, row.username, row.created_at]);
}

export function groupSimilarLogs(rows) {
  // 仅折叠排序后相邻的登记记录，保持展开后的原始顺序。相似不等于真实批次。
  const groups = [];
  for (const row of rows) {
    const previous = groups.at(-1), key = row.action === 'register' ? similarityKey(row) : null;
    if (key !== null && previous?.key === key) previous.rows.push(row);
    else groups.push({ key, rows: [row] });
  }
  return groups;
}

function actionLabel(action) {
  return el('span', { class: `activity-tag activity-${action}` }, actions[action].label);
}
function timeLabel(row) {
  const utc = `${row.created_at.replace(' ', 'T')}Z`;
  return el('time', { datetime: Number.isNaN(Date.parse(utc)) ? undefined : utc,
    title: `原始 UTC 时间：${row.created_at}`, text: logTime(row.created_at) });
}
function metadata(row) {
  return el('div', { class: 'activity-meta' }, el('span', {}, icon('users'), el('strong', { text: row.username })),
    el('span', { class: 'activity-time' }, timeLabel(row), el('small', { text: '上海时间' })));
}
function uuidControl(row, ctx) {
  const abbreviated = row.nfc_uuid.length > 20 ? `${row.nfc_uuid.slice(0, 8)}…${row.nfc_uuid.slice(-6)}` : row.nfc_uuid;
  const full = el('input', { type: 'text', value: row.nfc_uuid, readonly: true, 'aria-label': `日志 ${row.id} 的完整UUID` });
  const disclosure = el('details', { class: 'activity-uuid-full' }, el('summary', { text: '查看完整UUID' }),
    el('div', { class: 'activity-uuid-value' }, full, button('选中完整UUID', () => { full.focus(); full.select(); }, 'quiet')));
  const status = el('span', { class: 'activity-copy-status', role: 'status', 'aria-live': 'polite' });
  const copy = button('复制UUID', async () => {
    if (copy.disabled || !ctx.current()) return;
    copy.disabled = true; status.textContent = '';
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(row.nfc_uuid);
      if (ctx.current()) status.textContent = '已复制完整UUID';
    } catch {
      if (ctx.current()) { disclosure.open = true; status.textContent = '自动复制不可用，请选中完整UUID后手动复制。'; }
    } finally { copy.disabled = false; }
  }, 'quiet activity-copy');
  return el('div', { class: 'activity-uuid' }, el('div', { class: 'activity-uuid-line' },
    el('span', { class: 'activity-uuid-caption', text: 'NFC UUID' }), el('code', { text: abbreviated }), copy), disclosure, status);
}
function recordCard(row, ctx) {
  return el('article', { class: `activity-record activity-record-${row.action}`, 'data-log-id': row.id },
    el('div', { class: 'activity-record-head' }, el('span', { class: `activity-symbol activity-${row.action}` }, icon(actions[row.action].icon)),
      el('div', { class: 'activity-record-main' }, el('div', { class: 'activity-title-line' },
        el('h3', { text: row.medicine_name }), actionLabel(row.action)), metadata(row)),
      el('div', { class: 'activity-quantity' }, el('span', { text: '涉及数量' }), el('strong', { text: `${row.quantity} 支` }))),
    uuidControl(row, ctx), el('div', { class: 'activity-record-ref', text: `日志 #${row.id} · 药品 #${row.medicine_id}` }));
}
function groupedCard(group, ctx) {
  const row = group.rows[0], toggle = el('span', { class: 'activity-group-toggle', text: `展开 ${group.rows.length} 条原始记录` });
  const details = el('details', { class: 'activity-group' },
    el('summary', {}, el('span', { class: 'activity-symbol activity-register' }, icon('plus')),
      el('span', { class: 'activity-group-main' }, el('span', { class: 'activity-title-line' },
        el('strong', { class: 'activity-group-name', text: row.medicine_name }),
        el('span', { class: 'activity-tag activity-register', text: `登记记录 ${group.rows.length} 条` })),
        metadata(row), el('span', { class: 'activity-group-note', text: '相似记录分组 · 非实际批次' })), toggle),
    el('div', { class: 'activity-group-records' }, group.rows.map(item => recordCard(item, ctx))));
  details.addEventListener('toggle', () => { toggle.textContent = details.open ? '收起原始记录' : `展开 ${group.rows.length} 条原始记录`; });
  return details;
}

export async function renderOperationLogs(ctx) {
  const user = ctx.user(), page = el('div', { class: 'activity-page' });
  let exporting = false;
  const exportError = alertBox(), exportStatus = el('span', { role: 'status', 'aria-live': 'polite' });
  const canExport = ['admin', 'staff'].includes(user.role);
  const exportScope = canExport ? el('div', { class: 'activity-export-scope' },
    el('p', { text: `CSV导出范围：${user.role === 'admin' ? '当前账号有权查看的全部日志' : '本人全部日志'}，不受页面筛选、排序或相似分组折叠影响。药品名称为当前名称。` }), exportStatus, exportError) : null;
  const exportButton = canExport ? button('导出日志 CSV', async () => {
    if (exporting || !ctx.current()) return;
    exporting = true; exportStatus.textContent = '正在生成并下载日志 CSV…'; exportError.textContent = '';
    try {
      const result = await ctx.run(exportButton, async () => {
        const file = await ctx.request('/api/export/logs', { file: true });
        if (!ctx.current()) return;
        const url = URL.createObjectURL(file.blob);
        const link = el('a', { href: url, download: file.filename });
        try { document.body.append(link); link.click(); }
        finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
      }, { permit: ['admin', 'staff'], errorTarget: exportError });
      if (ctx.current()) {
        exportStatus.textContent = '';
        if (result.ok) ctx.toast('日志 CSV 已交给浏览器下载，包含当前账号权限范围内的全部原始日志。');
        else exportError.textContent = result.error?.message || '日志导出未成功，请稍后重试。';
      }
    } finally {
      exporting = false;
      if (ctx.current()) exportButton.replaceChildren(icon('download'), '导出日志 CSV');
    }
  }, '', 'download') : null;
  const title = heading('操作日志', '逐条留痕，清晰核对每一次登记与领取。', [exportButton, button('刷新日志', ctx.reload, '', 'refresh')], 'ACTIVITY LOG');
  page.append(title); ctx.root.replaceChildren(page);
  if (!['admin', 'staff'].includes(user.role)) { page.append(alertBox('当前账号无权查看操作日志。')); return; }
  page.append(exportScope, loading());
  const parameters = ctx.parameters || {};
  // API 仅支持这些参数；名称、排序等前端条件绝不能作为未知参数传给后端。
  const server = { user_id: user.role === 'admin' ? parameters.user_id : undefined,
    medicine_id: parameters.medicine_id, action: parameters.action };
  let rows;
  try {
    rows = records(await ctx.request(`/api/logs${query(server)}`), logRecord);
    if (!ctx.current()) return;
    if (new Set(rows.map(row => row.id)).size !== rows.length ||
        (user.role === 'staff' && rows.some(row => row.user_id !== user.id))) {
      throw new ApiError('日志数据或权限范围异常，请重新登录后读取。', 0, 'contract_error');
    }
  } catch (error) {
    if (!ctx.current() || ['aborted', 'stale_session'].includes(error.code)) return;
    page.replaceChildren(title, exportScope, el('section', { class: 'activity-load-error panel' },
      alertBox(`日志读取失败：${error.message || '请检查连接后重试。'}`), button('重新读取日志', ctx.reload, 'primary', 'refresh')));
    return;
  }
  const name = textInput('log_name', '输入药品名称，支持部分名称');
  const action = select('log_action', actionOptions);
  const users = [...new Map(rows.map(row => [row.user_id, row.username])).entries()].sort((a, b) => a[1].localeCompare(b[1], 'zh-CN') || a[0] - b[0]);
  const actor = select('log_actor', [['', '全部已加载操作人'], ...users.map(([id, username]) => [String(id), `${username} · #${id}`])]);
  const order = select('log_order', [['newest', '时间从新到旧'], ['oldest', '时间从旧到新']], 'newest');
  const collapse = el('input', { type: 'checkbox', checked: true });
  const resultNote = el('p', { class: 'activity-result-note', role: 'status', 'aria-live': 'polite' });
  const list = el('div', { class: 'activity-list', 'aria-label': '操作日志记录' });
  const chips = el('div', { class: 'activity-chips', 'aria-label': '快捷操作筛选' });
  const shortcuts = [['', '全部记录'], ['dispense', '领取记录'], ['register', '登记记录']].map(([key, text]) => {
    const count = key ? rows.filter(row => row.action === key).length : rows.length;
    const target = button(`${text} ${count}`, () => { action.value = key; update(); }, `activity-chip activity-chip-${key || 'all'}`);
    target.setAttribute('aria-pressed', String(key === '')); chips.append(target); return [key, target];
  });
  const serverScope = [server.user_id ? `用户 #${server.user_id}` : '', server.medicine_id ? `药品 #${server.medicine_id}` : '',
    server.action ? actions[server.action]?.label : ''].filter(Boolean);
  const scope = el('div', { class: 'activity-scope' }, icon('shield'), el('div', {},
    el('strong', { text: user.role === 'admin' ? `管理员 · 已加载 ${rows.length} 条权限内记录` : `仅本人记录 · ${user.username} · 已加载 ${rows.length} 条` }),
    el('p', { text: '名称、类型、操作人筛选及排序仅作用于本次已加载记录；刷新日志可读取最新数据。' }),
    serverScope.length ? el('p', { text: `当前服务器读取范围：${serverScope.join(' · ')}。清除筛选可重新读取完整权限范围。` }) : null));
  const clear = button('清除筛选', () => {
    if (serverScope.length) { ctx.navigate('logs'); return; }
    name.value = ''; name.dispatchEvent(new Event('input')); action.value = ''; actor.value = ''; order.value = 'newest'; collapse.checked = true; update();
  }, 'quiet');
  const filters = el('section', { class: 'panel activity-filters', 'aria-label': '日志筛选' },
    el('div', { class: 'activity-filter-grid' }, field('药品名称', name), field('操作类型', action),
      user.role === 'admin' ? field('操作人', actor) : el('div', { class: 'activity-self' }, el('span', { text: '操作人' }), el('strong', { text: `${user.username}（本人）` })),
      field('时间排序', order)),
    el('div', { class: 'activity-filter-bottom' }, el('label', { class: 'activity-grouping' }, collapse, '折叠相似登记记录'), clear),
    el('p', { class: 'activity-rule', text: '分组条件：相邻登记记录的药名、操作人和原始 UTC 时间（同一秒）均相同。仅折叠显示，不代表同一真实批次。' }));

  // 保留原有编号查询入口；它会请求服务器，区别于上方已加载记录内的筛选。
  const userID = textInput('user_id', '用户内部 ID', String(server.user_id || '')); userID.inputMode = 'numeric';
  const medicineID = textInput('medicine_id', '药品内部 ID', String(server.medicine_id || '')); medicineID.inputMode = 'numeric';
  const rangeError = alertBox();
  const rangeForm = el('form', { class: 'activity-range-form' }, user.role === 'admin' ? field('按用户 ID 读取', userID) : null,
    field('按药品 ID 读取', medicineID), el('button', { type: 'submit', class: 'btn', text: '按编号读取日志' }), rangeError);
  rangeForm.addEventListener('submit', event => {
    event.preventDefault();
    try { ctx.navigate('logs', { user_id: user.role === 'admin' ? positiveFilter(userID.value.trim()) : undefined,
      medicine_id: positiveFilter(medicineID.value.trim()), action: action.value || undefined }); }
    catch (error) { rangeError.textContent = error.message; }
  });
  const range = el('details', { class: 'activity-range' }, el('summary', { text: '按编号读取日志（可选）' }), rangeForm);
  page.replaceChildren(title, exportScope, scope, chips, filters, range,
    el('div', { class: 'activity-results-head' }, el('h2', { text: '记录明细' }), resultNote), list,
    el('p', { class: 'activity-footnote', text: '时间按上海时区显示，原始时间为 UTC；悬停时间可查看原值。药品名称为当前关联名称，非历史快照。资料编辑暂不产生日志；启停记录的数量不代表库存增减。' }));

  function update() {
    if (!ctx.current()) return;
    const term = name.value.trim().toLocaleLowerCase();
    const direction = order.value === 'oldest' ? 1 : -1;
    const visible = rows.filter(row => (!term || row.medicine_name.toLocaleLowerCase().includes(term)) &&
      (!action.value || row.action === action.value) && (!actor.value || String(row.user_id) === actor.value))
      .sort((a, b) => direction * ((a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0) || a.id - b.id));
    const groups = collapse.checked ? groupSimilarLogs(visible) : visible.map(row => ({ rows: [row] }));
    const folded = groups.filter(group => group.rows.length > 1).length;
    resultNote.textContent = `显示 ${visible.length} 条原始日志 / 已加载 ${rows.length} 条${folded ? ` · ${folded} 组相似登记记录` : ''}`;
    shortcuts.forEach(([key, target]) => target.setAttribute('aria-pressed', String(action.value === key)));
    list.replaceChildren(...(visible.length ? groups.map(group => group.rows.length > 1 ? groupedCard(group, ctx) : recordCard(group.rows[0], ctx)) :
      [empty(rows.length ? '没有符合条件的日志' : '暂无操作日志', rows.length ? '调整或清除筛选条件，查看当前已加载的其他记录。' : '发生登记、领取或启停操作后，这里会显示真实日志。')]));
  }
  name.addEventListener('input', update); [action, actor, order, collapse].forEach(control => control.addEventListener('change', update));
  update();
}
