import { api, ApiError } from './api.js';
import { auth, roles } from './auth.js';
import { el, icon, button, field, textInput, alertBox, loading } from './ui.js';
import { renderDashboard, renderMedicines, renderInventory, renderNFC, renderLogs, renderUsers } from './views.js?v=macos-20261024';

const root = document.getElementById('app');
const routes = {
  dashboard: ['系统总览', 'dashboard', renderDashboard], medicines: ['药品管理', 'medicine', renderMedicines],
  inventory: ['库存管理', 'inventory', renderInventory], nfc: ['NFC 识别与领取', 'nfc', renderNFC],
  logs: ['操作日志', 'logs', renderLogs], users: ['用户管理', 'users', renderUsers],
};
let revision = 0, controller = null, writing = 0, currentPage = 'dashboard', currentParameters = {};
let shell, main, navigation, userArea, breadcrumb, sidebarAccount;

// 仅管理导航的展示状态，不改变访问权限或业务请求。
function setMenu(open) {
  const wasOpen = shell.classList.contains('menu-open');
  shell.classList.toggle('menu-open', open);
  const trigger = shell.querySelector('.mobile-menu');
  trigger.setAttribute('aria-expanded', String(open));
  if (open) shell.querySelector('.mobile-close').focus({ preventScroll: true });
  else if (wasOpen) trigger.focus({ preventScroll: true });
}

function toast(message, error = false) {
  const item = el('div', { class: `toast ${error ? 'error' : ''}`, text: message });
  document.getElementById('notifications').append(item);
  setTimeout(() => item.remove(), 6500);
}
function closeDialogs() { document.querySelectorAll('dialog').forEach(node => { node.close(); node.remove(); }); }
function allowed(page) {
  return Boolean(auth.user()) && (page !== 'users' || auth.user().role === 'admin') && (page !== 'logs' || auth.user().role !== 'viewer');
}
function brand() {
  return el('div', { class: 'brand' }, el('span', { class: 'brand-mark', 'aria-hidden': 'true', text: '+' }),
    el('div', {}, '影像科急救药品', el('div', { class: 'brand-caption', text: '药品管理工作台' })));
}
function updateUser() {
  if (!auth.user() || !navigation) return;
  const user = auth.user();
  sidebarAccount.replaceChildren(el('span', { class: 'avatar', 'aria-hidden': 'true', text: [...user.username][0] || 'U' }),
    el('div', { class: 'sidebar-account-info' }, el('strong', { title: user.username, text: user.username }),
      el('span', { text: roles[user.role] })));
  navigation.replaceChildren(...Object.entries(routes).filter(([key]) => allowed(key)).map(([key, [title, symbol]]) =>
    button(title, () => navigate(key), `nav-item ${currentPage === key ? 'active' : ''}`, symbol)));
  [...navigation.children].forEach((node, index) => {
    const key = Object.keys(routes).filter(allowed)[index];
    if (key === currentPage) node.setAttribute('aria-current', 'page');
  });
  userArea.replaceChildren(el('span', { class: 'avatar', 'aria-hidden': 'true', text: [...user.username][0] || 'U' }),
    el('div', {}, el('div', { class: 'user-name', title: user.username, text: user.username }), el('div', { class: 'user-role', text: roles[user.role] })),
    el('span', { class: 'top-divider' }), button('退出', () => {
      if (writing) toast('操作正在提交，请核实结果后再退出。');
      else auth.logout();
    }, 'quiet small', 'logout'));
}

function showLogin(reason = '') {
  revision += 1; controller?.abort(); writing = 0; closeDialogs();
  document.getElementById('notifications').replaceChildren();
  navigation = null; main = null;
  const username = textInput('username', '请输入用户名'); username.autocomplete = 'username'; username.required = true;
  const password = el('input', { name: 'password', type: 'password', autocomplete: 'current-password', placeholder: '请输入密码', required: true, maxLength: 2048 });
  const error = alertBox(reason);
  const submit = el('button', { class: 'btn primary wide', type: 'submit' }, '登录系统', icon('arrow'));
  const form = el('form', { class: 'form-stack' }, field('用户名', username), field('密码', password), submit);
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (submit.disabled) return;
    submit.disabled = true; submit.textContent = '正在验证身份…'; error.textContent = '';
    try {
      await auth.login(username.value, password.value);
      mountShell(); await navigate('dashboard', {}, true);
    } catch (failure) {
      if (form.isConnected && !['stale_session', 'aborted'].includes(failure.code)) error.textContent = failure.message;
    } finally {
      password.value = ''; submit.disabled = false; submit.replaceChildren('登录系统', icon('arrow'));
    }
  });
  root.replaceChildren(el('div', { class: 'login' },
    el('main', { id: 'main-content', class: 'login-pane' }, el('section', { class: 'login-form', 'aria-labelledby': 'login-title' },
      brand(), el('div', { class: 'login-intro' }, el('h1', { id: 'login-title', text: '登录工作台' }),
        el('p', { text: '让每一支药品，都有迹可循。' })),
      error, form, el('div', { class: 'login-note' }, icon('shield'), '密码不会保存在网页中；刷新页面后需要重新登录。')),
      el('p', { class: 'login-foot', text: '医学影像科 · 急救药品管理系统' }))));
}

function mountShell() {
  const scrim = el('button', { class: 'nav-scrim', 'aria-label': '关闭导航', onClick: () => setMenu(false) });
  navigation = el('nav', { id: 'primary-navigation', class: 'nav-list', 'aria-label': '主要导航' });
  userArea = el('div', { class: 'user-area' });
  sidebarAccount = el('div', { class: 'sidebar-account', 'aria-label': '当前账号' });
  breadcrumb = el('strong', { text: '系统总览' });
  main = el('main', { id: 'main-content', class: 'main', tabindex: '-1' });
  shell = el('div', { class: 'shell' }, scrim,
    el('aside', { class: 'sidebar' }, brand(), button('关闭', () => setMenu(false), 'quiet small mobile-close'),
      el('div', { class: 'nav-label', text: '工作空间' }), navigation,
      el('div', { class: 'sidebar-bottom' }, el('div', { class: 'sidebar-note' }, icon('shield'), el('span', { text: '一支药品 · 一个 UUID' })), sidebarAccount)),
    el('div', { class: 'workspace' }, el('header', { class: 'topbar' },
      button('', () => setMenu(!shell.classList.contains('menu-open')), 'mobile-menu icon-button', 'menu'),
      el('div', { class: 'topbar-context' }, '药品管理工作台', '/', breadcrumb), userArea), main));
  shell.querySelector('.mobile-menu').setAttribute('aria-label', '打开导航');
  shell.querySelector('.mobile-menu').setAttribute('aria-expanded', 'false');
  shell.querySelector('.mobile-menu').setAttribute('aria-controls', 'primary-navigation');
  root.replaceChildren(shell); updateUser();
}

async function navigate(page, parameters = {}, force = false) {
  if (!auth.user()) return;
  if (writing && !force) { toast('操作正在提交，请稍候。'); return; }
  if (!routes[page]) page = 'dashboard';
  controller?.abort(); controller = new AbortController();
  const signal = controller.signal, mine = ++revision, session = auth.epoch();
  currentPage = page; currentParameters = parameters;
  closeDialogs(); setMenu(false); main.dataset.page = page;
  main.replaceChildren(loading()); main.setAttribute('aria-busy', 'true');
  window.scrollTo(0, 0);
  const context = {
    root: main, signal, parameters,
    current: () => revision === mine && auth.epoch() === session && Boolean(auth.user()),
    user: () => auth.user(), navigate, toast,
    reload: () => navigate(page, parameters),
    request: (path, options = {}) => api.request(path, { signal, ...options }),
    async run(target, action, { write = false, permit = null, errorTarget = null } = {}) {
      if (target.disabled || !context.current()) return { ok: false };
      if (writing) { toast('另一个操作正在提交，请稍候。'); return { ok: false }; }
      target.disabled = true; const label = target.textContent;
      target.textContent = write ? '正在提交…' : '正在处理…';
      if (write) writing += 1;
      if (errorTarget) errorTarget.textContent = '';
      try {
        const previousRole = auth.user()?.role;
        const user = await auth.sync(write ? undefined : signal);
        if (!context.current()) return { ok: false };
        updateUser();
        if (user.role !== previousRole) {
          toast('账号权限已更新，页面已重新读取。');
          await navigate(allowed(page) ? page : 'dashboard', parameters, true);
          return { ok: false };
        }
        if (permit && !permit.includes(user.role)) throw new ApiError('当前账号没有执行此操作的权限。', 403, 'forbidden');
        return { ok: true, value: await action() };
      } catch (failure) {
        if (context.current() && !['aborted', 'stale_session'].includes(failure.code)) {
          if (errorTarget) errorTarget.textContent = failure.message;
          else toast(failure.message, true);
          if (failure.status === 403) {
            const oldRole = auth.user()?.role;
            try {
              await auth.sync(signal); updateUser();
              if (context.current() && auth.user().role !== oldRole) navigate(allowed(page) ? page : 'dashboard', {}, true);
            } catch (error) { if (context.current() && error.status !== 401) toast('权限状态刷新失败，请刷新页面。', true); }
          }
        }
        return { ok: false, error: failure };
      } finally {
        if (write && auth.epoch() === session) writing = Math.max(0, writing - 1);
        target.disabled = false; target.textContent = label;
      }
    },
  };
  try {
    await auth.sync(signal);
    if (!context.current()) return;
    updateUser();
    if (!allowed(page)) throw new ApiError('当前账号不能访问此页面。', 403, 'forbidden');
    breadcrumb.textContent = routes[page][0];
    await routes[page][2](context);
    if (context.current()) {
      main.append(el('footer', { class: 'page-foot' }, el('span', { text: '影像科急救药品管理 · 数据来自本地数据库' }),
        el('span', { text: `最近读取 ${new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date())} · 上海时间` })));
      window.scrollTo(0, 0);
    }
  } catch (failure) {
    if (context.current() && !['aborted', 'stale_session'].includes(failure.code)) {
      main.replaceChildren(el('div', { class: 'panel page-error' }, alertBox(failure.message),
        button('重新读取', () => navigate(page, parameters), '', 'refresh'), button('返回总览', () => navigate('dashboard'), 'quiet')));
    }
  } finally { if (context.current()) main.setAttribute('aria-busy', 'false'); }
}

auth.subscribe(reason => showLogin(reason));
// 恢复到页面时校验服务器当前角色；不保存上一用户的数据。
async function recheckRole() {
  if (!auth.user() || document.hidden || writing) return;
  const previous = auth.user().role, session = auth.epoch();
  try {
    await auth.sync(); updateUser();
    if (auth.user() && auth.user().role !== previous) { closeDialogs(); await navigate(allowed(currentPage) ? currentPage : 'dashboard', {}, true); }
  } catch (error) { if (auth.user() && auth.epoch() === session) toast('身份状态暂时无法更新，操作前将再次核验。', true); }
}
window.addEventListener('focus', recheckRole);
document.addEventListener('visibilitychange', () => { if (!document.hidden) recheckRole(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape' && shell?.classList.contains('menu-open')) setMenu(false); });
showLogin();
