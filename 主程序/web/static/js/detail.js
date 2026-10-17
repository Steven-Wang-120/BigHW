'use strict';
(async () => {
  const message = appApi.$('message'), detail = appApi.$('detail'), params = new URLSearchParams(location.search);
  appApi.$('difference-id').value = params.get('difference_id') || '';
  let current, user, busy = false;
  const back = appApi.link(detail.parentNode, '返回差异列表', '/differences?' + new URLSearchParams({run_id: params.get('run_id') || ''}));
  function buttons() {
    const owned = user.role === 'HANDLER' && current?.assignee_id === user.id;
    appApi.$('actions').hidden = !current;
    appApi.$('assign').hidden = appApi.$('handler').parentNode.hidden = user.role !== 'SUPERVISOR';
    appApi.$('assign').disabled = busy || current?.work_status !== 'PENDING' || !!current?.assignee_id || !appApi.$('handler').value;
    appApi.$('start').hidden = appApi.$('close').hidden = appApi.$('note').parentNode.hidden = user.role !== 'HANDLER';
    appApi.$('start').disabled = busy || !owned || current?.work_status !== 'PENDING';
    appApi.$('close').disabled = busy || !owned || current?.work_status !== 'IN_PROGRESS';
  }
  async function load() {
    current = (await appApi.request(`/differences/${encodeURIComponent(appApi.$('difference-id').value.trim())}`)).data;
    detail.replaceChildren();
    const summary = appApi.element('section', undefined, detail); summary.className = 'summary';
    appApi.element('h2', `${appApi.label('reason', current.type)} · ${appApi.label('status', current.work_status)}`, summary);
    appApi.element('p', `匹配结论：${appApi.label('match', current.match_status)}；数量：${current.quantity}；金额：${appApi.money(current.amount_units)}；版本：${current.version}；处理人：${current.assignee_id || '未派单'}`, summary);
    appApi.element('p', current.message, summary);
    appApi.element('p', '关闭工单不会改变匹配结论；规则结果是当前范围内推断，不代表真实原单。', summary);
    const source = current.source;
    appApi.element('p', `来源质量：${appApi.qualityText(source.quality)}；源行flags：${(source.flags || []).join('、') || '无已记录标记'}；工单flags：${current.flags.join('、') || '无'}`, summary);
    appApi.table(detail, '来源行', ['Sheet / 原行号', '单号 / 商品', '客户 / 国家', '时间', '数量', '单价 / 金额'], [[`${source.sheet} / ${source.source_row}`, `${source.invoice} / ${source.stock} · ${source.description || ''}`, `${source.customer || '未提供'} / ${source.country || '未提供'}`, source.invoice_time, source.quantity, `${appApi.money(source.price_units)} / ${appApi.money(source.amount_units)}`]]);
    appApi.table(detail, '当前范围候选（推断，非真实原单）', ['候选批次', '来源行', '时间 / 国家', '单价', '剩余数量', '同价 / 标记'], current.candidates.map(c => [c.lot_id, c.source_refs.map(r => typeof r === 'string' ? r : `${r.sheet}:${r.source_row}`).join('、'), `${c.invoice_time} / ${c.countries.join('、')}`, appApi.money(c.price_units), c.remaining_quantity, `${c.exact_price ? '是' : '否'} / ${c.flags.join('、') || '无'}`]));
    appApi.table(detail, '工单历史', ['时间', '操作人', '操作', '状态 / 版本', '说明 / request_id'], current.history.map(h => [h.created_at, h.actor_id, ({ASSIGN: '派单', START: '开始处理', CLOSE: '关闭'})[h.action] || h.action, `${appApi.label('status', h.states.after?.work_status)} / ${h.states.after?.version}`, `${h.states.after?.resolution_note || ''} / ${h.request_id}`]));
    appApi.jsonDetails(detail, current);
    const backParams = new URLSearchParams(params); backParams.delete('difference_id'); backParams.set('run_id', current.run_id); back.href = '/differences?' + backParams;
    buttons();
  }
  async function mutate(button) {
    if (!current || button.disabled) return;
    busy = true; buttons(); appApi.status('处理中，请稍候…');
    const event = button.id;
    const body = event === 'assign' ? {assignee_id: appApi.$('handler').value} : event === 'close' ? {resolution_note: appApi.$('note').value} : {};
    try {
      await appApi.request(`/differences/${encodeURIComponent(current.id)}/${event}`, appApi.json('POST', {...body, expected_version: current.version}));
      await load(); appApi.status('操作完成，已读取最新版本。');
    } catch (error) {
      if (error.status === 409) {
        try { await load(); appApi.showError(message, error); message.textContent += ' 已刷新最新版本、归属和状态；请重新审阅后手动操作，未自动重试。处理说明已保留。'; }
        catch (refreshError) {
          current = null; detail.replaceChildren();
          appApi.showError(message, error); message.textContent += ` 最新详情读取失败：${refreshError.payload?.error?.code || refreshError.message}（刷新request_id=${refreshError.payload?.request_id || '未取得'}），请重新读取；未自动重试，说明已保留。`;
        }
      } else appApi.showError(message, error);
    } finally { busy = false; buttons(); }
  }
  try {
    user = await appApi.me(['FINANCE', 'SUPERVISOR', 'HANDLER']);
    if (user.role === 'SUPERVISOR') {
      const handlers = (await appApi.request('/users/handlers')).data.items, select = appApi.$('handler');
      select.replaceChildren(); handlers.forEach(item => { const option = appApi.element('option', item.name, select); option.value = item.id; });
      select.onchange = buttons;
    }
    appApi.$('detail-form').addEventListener('submit', event => { event.preventDefault(); appApi.action(event.target.querySelector('button'), load); });
    ['assign', 'start', 'close'].forEach(id => { appApi.$(id).onclick = () => mutate(appApi.$(id)); });
    if (appApi.$('difference-id').value) await load();
  } catch (error) { appApi.showError(message, error); }
})();
