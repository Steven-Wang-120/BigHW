/* Same-origin session API. No token storage; cookie is read for every write. */
'use strict';
window.appApi = (() => {
  const $ = id => document.getElementById(id);
  function element(tag, text, parent) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (parent) parent.append(node);
    return node;
  }
  const messages = {403: '权限或同源校验失败。', 422: '请检查字段。', 429: '请求过于频繁，请稍后重试。', 503: '服务暂忙，请保留输入，稍后重试。'};
  function showError(node, error, options = {}) {
    const fields = error.payload?.error?.details?.fields || [];
    node.textContent = `${messages[error.status] || ''} ${error.payload?.error?.code || 'NETWORK_ERROR'}：${error.message} ${fields.length ? '字段：' + fields.join(', ') : ''} request_id=${error.payload?.request_id || '未取得'}`;
    node.className = 'error';
    if (error.status === 401 && options.redirect !== false && location.pathname !== '/login' && location.pathname !== '/') location.assign('/login');
  }
  function status(text) { const node = $('message'); if (node) { node.className = ''; node.textContent = text; } }
  function csrf() {
    const item = document.cookie.split('; ').find(v => v.startsWith('csrf_token='));
    return item ? decodeURIComponent(item.slice('csrf_token='.length)) : '';
  }
  // Unsafe *integer tokens* become decimal strings before the values are bound.
  // JSON.parse first validates the original grammar; its rounded result is discarded.
  // Strings (including property names and escapes) are copied without inspection.
  function parseJSON(text) {
    JSON.parse(text);
    let output = '', i = 0;
    while (i < text.length) {
      const start = i;
      if (text[i] === '"') {
        i++;
        while (i < text.length) {
          if (text[i] === '\\') { i += 2; continue; }
          if (text[i++] === '"') break;
        }
        output += text.slice(start, i);
      } else if (text[i] === '-' || /[0-9]/.test(text[i])) {
        const token = text.slice(i).match(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/)[0];
        i += token.length;
        const integer = !/[.eE]/.test(token);
        output += integer && (BigInt(token) > 9007199254740991n || BigInt(token) < -9007199254740991n) ? JSON.stringify(token) : token;
      } else { output += text[i++]; }
    }
    // Safe integers, decimals and exponents retain normal JSON Number semantics.
    return JSON.parse(output);
  }
  async function request(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (!['GET', 'HEAD'].includes(method)) headers.set('X-CSRF-Token', csrf());
    // Browser supplies Origin for same-origin writes. Never try to spoof Host/Origin.
    const response = await fetch('/api/v1' + path, {...options, method, headers, credentials: 'same-origin'});
    if (response.status === 204) return {response, data: null};
    if (!response.ok || (response.headers.get('content-type') || '').includes('json')) {
      const data = parseJSON(await response.text());
      if (!response.ok) { const error = new Error(data.error?.message || '请求失败'); error.payload = data; error.status = response.status; throw error; }
      return {response, data};
    }
    return {response};
  }
  const json = (method, value) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value)});
  async function action(button, operation, options = {}) {
    button.disabled = true;
    status('处理中，请稍候…');
    try { return await operation(); } catch (error) { showError($('message'), error); }
    finally { if (!options.keepDisabled) button.disabled = false; if ($('message')?.textContent === '处理中，请稍候…') status('操作完成。'); }
  }
  function link(parent, text, href) { const a = element('a', text, parent); a.href = href; return a; }
  function nav(user) {
    const node = $('nav'); if (!node) return;
    node.replaceChildren(); element('span', `${user.username} · ${user.role}`, node);
    const links = user.role === 'ADMIN' ? [['/admin', '用户与审计']] : [['/results', '运行'], ['/differences', '差异']];
    if (user.role === 'FINANCE') links.unshift(['/import', '导入']);
    if (['FINANCE', 'SUPERVISOR'].includes(user.role)) links.push(['/counts', '验算']);
    links.forEach(([href, label]) => link(node, label, href));
    const logout = element('button', '退出', node); logout.type = 'button';
    logout.onclick = () => action(logout, async () => { const response = await request('/auth/logout', {method: 'POST'}); if (response.response.status !== 204) throw new Error('注销未完成'); location.assign('/login'); });
  }
  async function me(roles) {
    const user = (await request('/auth/me')).data.user; nav(user);
    if (roles && !roles.includes(user.role)) { const error = new Error('当前角色不可操作此页面'); error.status = 403; throw error; }
    return user;
  }
  function money(units) {
    if (typeof units === 'number' && !Number.isSafeInteger(units)) throw new Error('金额不是安全整数；必须读取原始十进制文本，不能恢复已舍入Number。');
    const n = BigInt(String(units)), abs = n < 0n ? -n : n;
    const roundedPence = (abs + 50n) / 100n;
    const rounded = `${roundedPence / 100n}.${String(roundedPence % 100n).padStart(2, '0')}`;
    const exact = `${abs / 10000n}.${String(abs % 10000n).padStart(4, '0')}`;
    const sign = n < 0n ? '-' : '';
    return `${sign}£${rounded}（精确 £${sign}${exact}，${n} units）`;
  }
  const labels = {
    status: {RUNNING: '运行中', COMPLETED: '已完成', FAILED: '失败', PENDING: '待处理', IN_PROGRESS: '处理中', CLOSED: '已关闭'},
    reason: {MISSING_CUSTOMER: '缺少客户号', SUSPECTED_DUPLICATE: '疑似重复', COUNTRY_CONFLICT: '国家冲突', MULTIPLE_CANDIDATES: '候选过多', INSUFFICIENT_QUANTITY: '数量不足', PRICE_MISMATCH: '单价不一致', NO_CANDIDATE_IN_RANGE: '当前范围无候选', NON_PRODUCT_REVIEW: '非商品人工复核'},
    match: {UNMATCHED: '未匹配'}
  };
  function label(kind, value) { return labels[kind]?.[value] || value || '未提供'; }
  function jsonSafe(value) {
    if (typeof value === 'bigint') return value.toString();
    if (Array.isArray(value)) return value.map(jsonSafe);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, jsonSafe(v)]));
    return value;
  }
  function jsonDetails(parent, object) {
    const details = element('details', undefined, parent); element('summary', '技术详情（原始字段）', details); const pre = element('pre', undefined, details); pre.className = 'data compact'; pre.textContent = JSON.stringify(jsonSafe(object), null, 2); return details;
  }
  function facts(parent, object) {
    parent.replaceChildren(); const dl = element('dl', undefined, parent);
    Object.entries(object).forEach(([key, value]) => { element('dt', key, dl); const displayed = key === 'amount_units' || key === 'source_amount_units' ? money(value) : value; element('dd', typeof displayed === 'object' ? JSON.stringify(displayed, null, 2) : displayed ?? '未提供', dl); });
  }
  async function download(run, filters = {}) {
    status('生成授权CSV并审计中…');
    const query = new URLSearchParams({format: 'csv', ...filters});
    const {response} = await request(`/runs/${encodeURIComponent(run)}/export?${query}`);
    const blob = await response.blob(); // Server caps complete output at 32 MiB.
    if (blob.size > 32 * 1024 * 1024) throw new Error('导出超过客户端限制');
    const url = URL.createObjectURL(blob);
    const a = link(document.body, '保存CSV', url); a.download = 'authorized-differences.csv'; a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status(`服务器已生成 ${response.headers.get('X-Export-Rows')} 条授权记录。已请求浏览器保存，不代表用户已保存；request_id=${response.headers.get('X-Request-ID')}`);
  }
  // Every business page can navigate from authorized runs; IDs need not be copied.
  function appApiLabel(value) { return label('status', value); }

  async function runPicker(parent, onSelect, batch, options = {}) {
    const section = element('section', undefined, parent); section.className = 'card';
    element('h2', options.title || '选择可见运行', section);
    const list = element('div', undefined, section), pager = element('div', undefined, section);
    pager.className = 'pager';
    const prev = element('button', '上一页', pager), label = element('span', '', pager), next = element('button', '下一页', pager);
    let page = 1;
    async function load() {
      prev.disabled = next.disabled = true;
      const q = new URLSearchParams({page, page_size: 10}); if (batch) q.set('batch_id', batch);
      const data = (await request('/runs?' + q)).data;
      list.replaceChildren();
      if (data.items.length === 0) element('p', options.empty || '当前授权范围暂无运行。财务可先导入；处理人需要主管派单。', list);
      data.items.forEach(item => { const button = element('button', `${appApiLabel(item.status)} · attempt ${item.attempt} · ${item.run_id}`, list); button.type = 'button'; button.onclick = () => action(button, () => onSelect(item)); });
      label.textContent = `第 ${page} 页，共 ${data.total} 个授权运行`; prev.disabled = page <= 1; next.disabled = page * 10 >= data.total;
    }
    prev.onclick = () => action(prev, async () => { page--; await load(); }, {keepDisabled: true});
    next.onclick = () => action(next, async () => { page++; await load(); }, {keepDisabled: true});
    await load();
  }
  function table(parent, title, headers, rows) {
    element('h3', title, parent);
    if (!rows.length) { element('p', '当前范围未提供记录。', parent); return; }
    const table = element('table', undefined, parent), head = element('tr', undefined, element('thead', undefined, table));
    headers.forEach(text => element('th', text, head));
    const body = element('tbody', undefined, table);
    rows.forEach(values => { const row = element('tr', undefined, body); values.forEach(value => element('td', value ?? '未提供', row)); });
  }
  function qualityText(quality) {
    return quality?.provided ? `已验证 ${quality.quality_version}${quality.marked_row_count !== undefined ? ' · 标记 ' + quality.marked_row_count + ' 行' : ''}` : '未知：未提供逐行质量证据（不是0标记）';
  }
  return {$, element, showError, status, request, parseJSON, json, action, link, me, money, label, jsonDetails, facts, download, runPicker, table, qualityText};
})();
