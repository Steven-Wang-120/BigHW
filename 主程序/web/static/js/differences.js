'use strict';
(async () => {
  const message = appApi.$('message'), items = appApi.$('items'), initial = new URLSearchParams(location.search), size = 20;
  let page = /^[1-9][0-9]*$/.test(initial.get('page') || '') ? Number(initial.get('page')) : 1;
  function filters() {
    const result = {};
    [['type', 'type'], ['status', 'status'], ['assignee', 'assignee_id']].forEach(([id, key]) => { if (appApi.$(id).value) result[key] = appApi.$(id).value; });
    return result;
  }
  let requestSequence = 0;
  async function load() {
    const sequence = ++requestSequence;
    const snapshot = {run: appApi.$('run-id').value.trim(), filters: filters(), page};
    const queryButton = appApi.$('filter-form').querySelector('button');
    queryButton.disabled = appApi.$('prev').disabled = appApi.$('next').disabled = true;
    if (!snapshot.run) { items.replaceChildren(); appApi.$('page').textContent = ''; queryButton.disabled = false; appApi.status('请选择运行。'); return; }
    appApi.status('读取当前筛选中…');
    try {
      const query = new URLSearchParams({...snapshot.filters, page: snapshot.page, page_size: size});
      const data = (await appApi.request(`/runs/${encodeURIComponent(snapshot.run)}/differences?${query}`)).data;
      if (sequence !== requestSequence) return;
      const bookmark = new URLSearchParams({...snapshot.filters, run_id: snapshot.run, page: snapshot.page});
      history.replaceState(null, '', '/differences?' + bookmark);
      items.replaceChildren();
      if (!data.items.length) appApi.element('p', '当前授权筛选没有差异。', items);
      else {
        const table = appApi.element('table', undefined, items), head = appApi.element('tr', undefined, appApi.element('thead', undefined, table));
        ['差异原因', '匹配结论', '工单状态', '处理人 / 版本', '数量 / 金额', '详情'].forEach(v => appApi.element('th', v, head));
        const body = appApi.element('tbody', undefined, table);
        data.items.forEach(item => {
          const row = appApi.element('tr', undefined, body);
          [appApi.label('reason', item.type), appApi.label('match', item.match_status), appApi.label('status', item.work_status), `${item.assignee_id || '未派单'} / ${item.version}`, `${item.quantity} / ${appApi.money(item.amount_units)}`].forEach(v => appApi.element('td', v, row));
          appApi.link(appApi.element('td', undefined, row), '查看来源、候选与轨迹', '/detail?' + new URLSearchParams({...snapshot.filters, page: snapshot.page, run_id: snapshot.run, difference_id: item.id}));
        });
      }
      appApi.$('page').textContent = `第 ${snapshot.page} 页 / ${data.total} 条`;
      appApi.$('prev').disabled = snapshot.page <= 1; appApi.$('next').disabled = snapshot.page * size >= data.total;
      appApi.status('已载入当前授权筛选；导出使用相同筛选（不限当前页）。');
    } catch (error) {
      if (sequence === requestSequence) appApi.showError(message, error);
    } finally {
      // Stale responses (including failures) must not touch the newer UI state.
      if (sequence === requestSequence) queryButton.disabled = false;
    }
  }
  try {
    const user = await appApi.me(['FINANCE', 'SUPERVISOR', 'HANDLER']);
    ['MISSING_CUSTOMER', 'SUSPECTED_DUPLICATE', 'COUNTRY_CONFLICT', 'MULTIPLE_CANDIDATES', 'INSUFFICIENT_QUANTITY', 'PRICE_MISMATCH', 'NO_CANDIDATE_IN_RANGE', 'NON_PRODUCT_REVIEW'].forEach(v => { const option = appApi.element('option', appApi.label('reason', v), appApi.$('type')); option.value = v; });
    appApi.$('run-id').value = initial.get('run_id') || '';
    if (user.role === 'SUPERVISOR') {
      (await appApi.request('/users/handlers')).data.items.forEach(h => { const option = appApi.element('option', h.name, appApi.$('assignee')); option.value = h.id; });
    } else if (user.role === 'FINANCE') {
      // Finance may filter a known ID without requiring the supervisor-only directory.
      const input = document.createElement('input'); input.id = 'assignee'; input.placeholder = '处理人ID（可留空）'; appApi.$('assignee').replaceWith(input);
    } else appApi.$('assignee-label').hidden = true;
    [['type', 'type'], ['status', 'status'], ['assignee', 'assignee_id']].forEach(([id, key]) => { if (user.role !== 'HANDLER' || id !== 'assignee') appApi.$(id).value = initial.get(key) || ''; });
    appApi.$('filter-form').addEventListener('submit', event => { event.preventDefault(); page = 1; return load(); });
    ['type', 'status', 'assignee', 'run-id'].forEach(id => { appApi.$(id).onchange = () => { page = 1; return load(); }; });
    appApi.$('prev').onclick = () => { if (!appApi.$('prev').disabled) { page--; return load(); } };
    appApi.$('next').onclick = () => { if (!appApi.$('next').disabled) { page++; return load(); } };
    appApi.$('export').onclick = () => appApi.action(appApi.$('export'), () => appApi.download(appApi.$('run-id').value.trim(), filters()));
    if (appApi.$('run-id').value) await load();
    await appApi.runPicker(document.querySelector('.card'), async item => { appApi.$('run-id').value = item.run_id; page = 1; await load(); }, null, {title: '选择运行（无需手抄ID）'});
  } catch (error) { appApi.showError(message, error); }
})();
