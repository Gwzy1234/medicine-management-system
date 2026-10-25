// 所有不可信文本均通过 textContent / 文本节点插入，不使用 innerHTML。
import { roles } from './auth.js';

export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(attributes).forEach(([name, value]) => {
    if (value === undefined || value === null || value === false) return;
    if (name === 'class') node.className = value;
    else if (name === 'text') node.textContent = value;
    else if (name.startsWith('on')) node.addEventListener(name.slice(2).toLowerCase(), value);
    else if (['value', 'disabled', 'required', 'hidden', 'checked'].includes(name)) node[name] = value;
    else node.setAttribute(name, String(value));
  });
  children.flat(Infinity).forEach(child => { if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child))); });
  return node;
}
export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(svg.namespaceURI, 'use'); use.setAttribute('href', `/static/icons.svg#${name}`);
  svg.append(use); return svg;
}
export function button(text, action, type = '', symbol = null) {
  return el('button', { class: `btn ${type}`, type: 'button', onClick: action }, symbol ? icon(symbol) : null, text);
}
export function alertBox(text = '', type = 'error') { return el('div', { class: `alert ${type}`, role: 'alert', text }); }
let fieldNumber = 0;
export function field(label, input, hint = '') {
  const id = `field-${++fieldNumber}`;
  input.id = id;
  if (hint) input.setAttribute('aria-describedby', `${id}-hint`);
  return el('div', { class: 'field' }, el('label', { for: id, text: label }), input,
    hint ? el('small', { class: 'hint', id: `${id}-hint`, text: hint }) : null);
}
export function textInput(name, placeholder = '', value = '') {
  // HTML maxlength 按 UTF-16 计数；改用 Unicode 字符校验，避免静默截断合法输入。
  const input = el('input', { name, type: 'text', placeholder, value });
  const validate = () => {
    const trimmed = name === 'username' ? input.value.replace(/^[\s\u001c-\u001f]+|[\s\u001c-\u001f]+$/g, '') : input.value.trim();
    input.setCustomValidity([...trimmed].length > 500 ? '最多输入 500 个字符。' : '');
  };
  input.addEventListener('input', validate); validate();
  return input;
}
export function select(name, options, value = '') {
  const node = el('select', { name });
  options.forEach(([key, label]) => node.append(el('option', { value: key, text: label })));
  node.value = value; return node;
}
export function heading(title, subtitle, actions = [], eyebrow = '') {
  return el('div', { class: 'page-heading' }, el('div', {}, eyebrow ? el('div', { class: 'eyebrow', text: eyebrow }) : null,
    el('h1', { text: title }), el('p', { text: subtitle })), el('div', { class: 'button-row' }, actions));
}
export function panel(title, subtitle = '', action = null) {
  const body = el('div', { class: 'panel-body' });
  const root = el('section', { class: 'panel' }, el('div', { class: 'panel-head' }, el('div', {},
    el('h2', { text: title }), subtitle ? el('p', { text: subtitle }) : null), action), body);
  return { root, body };
}
export function empty(title = '暂无记录', message = '请调整查询条件，或稍后刷新。') {
  return el('div', { class: 'empty' }, icon('medicine'), el('h3', { text: title }), el('p', { text: message }));
}
export function loading() { return el('div', { class: 'loading', role: 'status', text: '正在读取最新数据…' }); }
export function table(columns, rows) {
  if (!rows.length) return empty();
  const body = el('tbody');
  rows.forEach(row => body.append(el('tr', {}, columns.map(([title, render]) => el('td', { 'data-label': title }, render(row))))));
  return el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, columns.map(([title]) => el('th', { scope: 'col', text: title })))), body));
}
export function badge(text, color = '') { return el('span', { class: `badge ${color}`, text }); }
export function today() {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  const part = type => parts.find(item => item.type === type).value;
  return `${part('year')}-${part('month')}-${part('day')}`;
}
// 只有服务器业务日期才能支持“已过期”判断；普通列表仍显示有效期、支持后端过期筛选。
// 没有服务器日期时省略辅助标记，不能用客户端时钟猜测业务状态。
export function expired(row) { return typeof row.business_date === 'string' && row.expires_on < row.business_date; }
export function states(row) {
  return el('div', { class: 'badge-stack' }, badge(row.status === 'in_stock' ? '在库' : '已领取', row.status === 'in_stock' ? 'blue' : ''),
    badge(row.is_active ? '启用' : '停用', row.is_active ? 'green' : ''), expired(row) ? badge('已过期', 'red') : null);
}
export function uuid(value, full = false) { return el('span', { class: `uuid ${full ? 'full' : ''}`, title: value, text: full || value.length < 18 ? value : `${value.slice(0, 8)}…${value.slice(-6)}` }); }
export function details(pairs) {
  return el('dl', { class: 'details' }, pairs.map(([key, value]) => el('div', { class: 'detail-pair' }, el('dt', { text: key }), el('dd', {}, value))));
}
export function medicineDetails(row) {
  return details([['药品 ID', String(row.id)], ['药品名称', row.name], ['NFC UUID', uuid(row.nfc_uuid, true)],
    ['存放位置', row.location], ['有效期', row.expires_on], ['状态', states(row)]]);
}
export function roleBadge(role) { return badge(roles[role], role === 'admin' ? 'blue' : ''); }
export function logTime(value) {
  const time = new Date(`${value.replace(' ', 'T')}Z`);
  return Number.isNaN(time.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(time);
}
export function dialog(title, content) {
  const node = el('dialog', { class: 'dialog', 'aria-label': title });
  let locked = false;
  const close = () => { if (!locked) { node.close(); node.remove(); } };
  node.append(el('div', { class: 'dialog-head' }, el('h2', { text: title }), button('关闭', close, 'quiet small')),
    el('div', { class: 'dialog-body' }, content));
  node.addEventListener('cancel', event => { event.preventDefault(); close(); });
  node.addEventListener('close', () => node.remove(), { once: true });
  document.body.append(node); node.showModal();
  return { node, close, lock(value) { locked = value; node.querySelectorAll('.dialog-head button').forEach(item => { item.disabled = value; }); } };
}
export function positiveFilter(value) {
  if (!value) return undefined;
  // 以字符串比较，避免 JavaScript Number 丢失 64 位 ID 的精度。
  const max = '9223372036854775807';
  if (!/^[1-9][0-9]*$/.test(value) || value.length > max.length || (value.length === max.length && value > max)) {
    throw new Error('筛选 ID 必须是 SQLite 范围内的正整数。');
  }
  return value;
}
