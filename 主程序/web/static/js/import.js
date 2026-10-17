'use strict';
(async () => {
  try {
    const user = await appApi.me(['FINANCE']);
    const select = appApi.$('snapshot'), digest = appApi.$('digest'), message = appApi.$('message');
    const snapshots = (await appApi.request('/snapshots?page=1&page_size=100')).data.items;
    snapshots.sort((a, b) => Number(b.recommended) - Number(a.recommended));
    snapshots.forEach(item => { const option = appApi.element('option', `${item.recommended ? '推荐 ' : ''}v${item.manifest_version} · ${item.snapshot_id.slice(0, 12)}… · ${item.sheet} ${item.start_row}-${item.end_row} · N=${item.original_total} · 质量：${appApi.qualityText(item.quality)}`, select); option.value = item.snapshot_id; option.dataset.digest = item.manifest_digest; });
    const choose = () => { digest.value = select.selectedOptions[0]?.dataset.digest || ''; };
    select.onchange = choose; choose();
    appApi.$('import-form').addEventListener('submit', event => { event.preventDefault(); appApi.action(event.target.querySelector('button'), async () => {
      const file = appApi.$('file').files[0]; if (!file) throw new Error('请选择CSV');
      appApi.status('上传、原值核验、导入和配对中…'); const form = new FormData(); form.append('snapshot_id', select.value); form.append('manifest_digest', digest.value); form.append('matching_rule_version', 'conservative-v1-90d'); form.append('file', file);
      try { const data = (await appApi.request('/pipeline', {method:'POST', body: form})).data; appApi.$('summary').textContent = `batch=${data.batch_id} run=${data.run_id} status=${data.status} attempt=${data.attempt}`; location.assign(`/results?run_id=${encodeURIComponent(data.run_id)}`); }
      catch (error) { appApi.showError(message, error); const detail = error.payload?.error?.details || {}; if (error.payload?.error?.code === 'SNAPSHOT_CONTENT_ALREADY_REGISTERED') message.textContent += ' 此库已登记相同CSV的旧快照；旧事实已保留。请由协调端配置新v4库或决定显式迁移，不会自动覆盖。'; if (detail.batch_id) { const retry = appApi.$('retry'); retry.hidden = false; retry.onclick = () => appApi.action(retry, async () => { const data = (await appApi.request('/runs', appApi.json('POST', {batch_id: detail.batch_id, matching_rule_version: 'conservative-v1-90d'}))).data; location.assign(`/results?run_id=${encodeURIComponent(data.run_id)}`); }); } }
    }); });
  } catch (error) { appApi.showError(appApi.$('message'), error); }
})();
