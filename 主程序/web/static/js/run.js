const $ = id => document.getElementById(id);
$('run').value = new URLSearchParams(location.search).get('run_id') || '';
async function load() {
  $('status').textContent = '读取中';
  for (const id of ['coverage','conservation','rule']) $(id).textContent = '';
  $('counts').removeAttribute('href');
  try {
    const response = await fetch('/evidence/run/' + encodeURIComponent($('run').value.trim()), {credentials:'same-origin'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.code || 'HTTP ' + response.status);
    $('status').textContent = 'COMPLETED 本地规则已对账 · 非真实原单准确率';
    const pick = keys => Object.fromEntries(keys.map(key => [key, data[key]]));
    $('coverage').textContent = JSON.stringify(pick(['product_valid_cancel','matched','suspected','no_original','match_rate','product_delta','effective_cancel_count','non_product_review','suspected_manual','coverage_delta','raw_c_prefix_count','excluded_c_prefix','raw_c_delta','difference_reasons']),null,2);
    $('conservation').textContent = JSON.stringify(pick(['cancel_quantity','allocated_quantity','unmatched_quantity','quantity_delta','cancel_amount_units','allocated_amount_units','unmatched_amount_units','amount_delta_units']),null,2);
    $('rule').textContent = JSON.stringify(pick(['run_id','batch_id','snapshot_id','normalization_version','rule_digest','rule','attempt','source_counts','truncation','row_truncation_flags_implemented','request_id']),null,2);
    $('counts').href = '/evidence/counts?batch_id=' + encodeURIComponent(data.batch_id);
  } catch(error) { $('status').textContent = '读取失败：' + error.message; }
}
$('load').addEventListener('click',load);
if ($('run').value) load();
