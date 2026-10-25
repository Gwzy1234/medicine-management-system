import { api, ApiError, userRecord } from './api.js';

let current = null, epoch = 0, expiryTimer = null;
const listeners = new Set();
export const roles = { admin: '管理员', staff: '工作人员', viewer: '只读用户' };
const publish = reason => listeners.forEach(listener => listener(reason));

export const auth = {
  user: () => current,
  epoch: () => epoch,
  subscribe(callback) { listeners.add(callback); return () => listeners.delete(callback); },
  logout(reason = '') {
    epoch += 1; current = null; clearTimeout(expiryTimer); api.setToken(null); publish(reason);
  },
  async login(username, password) {
    const mine = ++epoch;
    const result = await api.request('/api/auth/login', { method: 'POST', body: { username, password }, auth: false });
    if (mine !== epoch) throw new ApiError('会话已切换。', 0, 'stale_session');
    if (typeof result?.access_token !== 'string' || !Number.isFinite(result.expires_in) || result.expires_in <= 0) {
      throw new ApiError('登录响应异常，请重新登录。', 0, 'contract_error');
    }
    api.setToken(result.access_token);
    try {
      current = userRecord(await api.request('/api/auth/me'));
      if (mine !== epoch) throw new ApiError('会话已切换。', 0, 'stale_session');
      clearTimeout(expiryTimer);
      expiryTimer = setTimeout(() => auth.logout('登录已过期，请重新登录。'), result.expires_in * 1000);
      return current;
    } catch (error) {
      if (mine === epoch) auth.logout();
      throw error;
    }
  },
  async sync(signal) {
    if (!current) throw new ApiError('请先登录。', 401, 'unauthorized');
    const mine = epoch;
    const updated = userRecord(await api.request('/api/auth/me', { signal }));
    if (mine !== epoch) throw new ApiError('会话已切换。', 0, 'stale_session');
    current = updated;
    return current;
  },
};
api.onUnauthorized(() => auth.logout('登录已失效，请重新登录。'));
