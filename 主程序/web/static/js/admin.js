'use strict';
(async () => {
  const message = appApi.$('message'); let page = 1; const size = 20;
  try {
    await appApi.me(['ADMIN']);
    const users = appApi.$('users'), audit = appApi.$('audit');
    const showUsers = data => { users.replaceChildren(); if (!data.items.length) appApi.element('p', '暂无用户。', users); data.items.forEach(item => { const row = appApi.element('div', undefined, users); appApi.element('span', `${item.username} · ${item.role} · ${item.active ? '启用' : '停用'} · version ${item.version}`, row); if (item.username !== 'admin') { const button = appApi.element('button', item.active ? '停用' : '启用', row); button.type = 'button'; button.onclick = () => appApi.action(button, async () => { await appApi.request(`/users/${encodeURIComponent(item.id)}`, appApi.json('PATCH', {active: !item.active, expected_version: item.version})); await loadUsers(); }); } }); };
    const showAudit = data => { audit.replaceChildren(); if (!data.items.length) appApi.element('p', '暂无审计记录。', audit); data.items.forEach(item => appApi.element('p', `${item.action} · ${item.outcome} · ${item.target_type}/${item.target_id} · ${item.created_at}`, audit)); };
    const loadUsers = async () => showUsers((await appApi.request(`/users?page=${page}&page_size=${size}`)).data);
    const loadAudit = async () => { const query = new URLSearchParams({page: 1, page_size: 20}); const type = appApi.$('audit-type')?.value; const id = appApi.$('audit-id')?.value.trim(); if (type) query.set('target_type', type); if (id) query.set('target_id', id); showAudit((await appApi.request('/audit?' + query)).data); };
    appApi.$('user-form').addEventListener('submit', event => { event.preventDefault(); appApi.action(event.target.querySelector('button'), async () => { await appApi.request('/users', appApi.json('POST', {username: appApi.$('new-username').value, password: appApi.$('new-password').value, role: appApi.$('new-role').value})); appApi.$('new-password').value = ''; await loadUsers(); }); });
    appApi.$('audit-load').onclick = () => appApi.action(appApi.$('audit-load'), loadAudit);
    await loadUsers(); await loadAudit();
  } catch (error) { appApi.showError(message, error); }
})();
