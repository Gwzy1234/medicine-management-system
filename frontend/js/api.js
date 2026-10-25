// 唯一 HTTP 入口。令牌仅保存在闭包内，不落盘，不写入 URL，也不自动重试写操作。
export class ApiError extends Error {
  constructor(message, status = 0, code = 'network_error') {
    super(message); this.name = 'ApiError'; this.status = status; this.code = code;
  }
}

const messages = {
  400: '业务参数不合法，请检查输入。', 401: '登录已失效，请重新登录。',
  403: '当前账号没有执行此操作的权限。', 404: '未找到对应记录。',
  409: '记录状态已变化或与现有数据冲突，请刷新后重试。',
  422: '字段格式或参数不合法，请检查输入。',
  500: '服务器处理失败，结果未确认成功，请刷新核实。',
  503: '数据库暂时繁忙，请稍后手动重试。',
};

export function query(parameters = {}) {
  const search = new URLSearchParams();
  Object.entries(parameters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  });
  return search.size ? `?${search}` : '';
}

export function createClient({ fetcher = (...args) => fetch(...args), timeout = 20000 } = {}) {
  let token = null, generation = 0, unauthorized = () => {};
  return {
    setToken(value) { token = value; generation += 1; },
    onUnauthorized(callback) { unauthorized = callback; },
    async request(path, { method = 'GET', body, signal, auth = true, file = false } = {}) {
      if (!path.startsWith('/api/') || path.includes('://')) throw new ApiError('接口地址不合法。', 0, 'contract_error');
      const controller = new AbortController(), current = generation;
      const cancel = () => controller.abort();
      if (signal?.aborted) controller.abort();
      signal?.addEventListener('abort', cancel, { once: true });
      let timedOut = false;
      const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
      const headers = { Accept: file ? 'text/csv' : 'application/json' };
      if (auth && token) headers.Authorization = `Bearer ${token}`;
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      try {
        const response = await fetcher(path, {
          method, headers, body: body === undefined ? undefined : JSON.stringify(body),
          signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
        });
        if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
        // 旧账号请求不能在新账号会话中更新页面或触发退出。
        if (current !== generation) throw new ApiError('会话已切换。', 0, 'stale_session');
        if (!response.ok) {
          let error;
          try { error = (await response.json()).error; } catch { /* 使用安全的通用提示。 */ }
          if (response.status === 401 && auth && current === generation) unauthorized();
          throw new ApiError(error?.message || messages[response.status] || '请求未成功，请稍后重试。', response.status, error?.code || 'http_error');
        }
        if (file) {
          if (!(response.headers.get('content-type') || '').toLowerCase().startsWith('text/csv')) {
            throw new ApiError('导出接口未返回 CSV，下载已取消。', 0, 'contract_error');
          }
          const blob = await response.blob();
          if (current !== generation || signal?.aborted) throw new ApiError('会话或页面已切换。', 0, 'stale_session');
          // 药品下载保留原文件名；日志只接受服务器按业务时区生成的严格附件名。
          let filename = 'medicines.csv';
          if (path === '/api/export/logs') {
            const encoded = /filename\*=utf-8''([^;]+)/i.exec(response.headers.get('content-disposition') || '');
            try { filename = encoded ? decodeURIComponent(encoded[1].trim()) : ''; } catch { filename = ''; }
            if (!/^操作日志_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.csv$/.test(filename)) {
              throw new ApiError('日志附件文件名不符合约定，下载已取消。', 0, 'contract_error');
            }
          }
          return { blob, filename };
        }
        let result;
        try { result = await response.json(); } catch { throw new ApiError('服务器响应格式异常，请刷新核实。', 0, 'contract_error'); }
        if (current !== generation || signal?.aborted) throw new ApiError('会话或页面已切换。', 0, 'stale_session');
        if (!result || typeof result !== 'object' || !Object.hasOwn(result, 'data') || Object.hasOwn(result, 'error')) {
          throw new ApiError('服务器未返回有效业务结果，请刷新核实。', 0, 'contract_error');
        }
        return result.data;
      } catch (error) {
        if (error instanceof ApiError) throw error;
        if (error.name === 'AbortError') {
          if (!timedOut) throw new ApiError('请求已取消。', 0, 'aborted');
          throw new ApiError('请求超时，结果尚未确认。请刷新核实，勿重复提交。', 0, 'timeout');
        }
        throw new ApiError('网络连接失败，结果尚未确认。请检查连接并刷新核实，勿重复提交。');
      } finally {
        clearTimeout(timer); signal?.removeEventListener('abort', cancel);
      }
    },
  };
}

export const api = createClient();

function invalid() { throw new ApiError('返回数据不符合接口约定，请刷新核实。', 0, 'contract_error'); }
export function userRecord(value) {
  if (!value || !Number.isSafeInteger(value.id) || value.id <= 0 || typeof value.username !== 'string' ||
      !['admin', 'staff', 'viewer'].includes(value.role)) invalid();
  return value;
}
export function medicineRecord(value) {
  if (!value || !Number.isSafeInteger(value.id) || value.id <= 0 ||
      !['nfc_uuid', 'name', 'location', 'expires_on'].every(key => typeof value[key] === 'string') ||
      !['in_stock', 'dispensed'].includes(value.status) || ![0, 1].includes(value.is_active)) invalid();
  return value;
}
export function scanRecord(value) {
  medicineRecord(value);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value.business_date) || typeof value.can_dispense !== 'boolean') invalid();
  return value;
}
export function stockRecord(value) {
  if (!value || typeof value.name !== 'string' || !Number.isSafeInteger(value.stock) || value.stock < 0 ||
      !Number.isSafeInteger(value.available_stock) || value.available_stock < 0 || value.available_stock > value.stock) invalid();
  return value;
}
export function records(value, validate = item => item) {
  if (!Array.isArray(value)) invalid();
  return value.map(validate);
}
