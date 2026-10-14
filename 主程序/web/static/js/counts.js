const $ = id => document.getElementById(id);
$('batch').value = new URLSearchParams(location.search).get('batch_id') || '';
$('timestamp').textContent = new Date().toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'}) + ' +08';
async function load() {
  $('status').textContent = '读取中';
  for (const id of ['totals','categories','coverage','quality']) $(id).replaceChildren();
  try {
    const response = await fetch('/evidence/counts/' + encodeURIComponent($('batch').value.trim()), {credentials:'same-origin'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.code || 'HTTP ' + response.status);
    $('status').textContent = 'SUCCESS 导入事实已发布 · 对账状态请看该批次run';
    $('runs').replaceChildren();
    for (const run of data.runs || []) {
      const a = document.createElement('a');
      a.href = '/evidence/run?run_id=' + encodeURIComponent(run.run_id);
      a.textContent = 'run ' + run.run_id + ' · ' + run.status;
      $('runs').append(a, document.createElement('br'));
    }
    if (!(data.runs || []).length) $('runs').textContent = '该批次尚无run；需显式本地reconcile。';
    const labels = {original_total:'完整扫描原文件 N', outside_count:'范围外', selected_count:'选中源位置', sale_count:'正向有效 SALE',cancel_count:'取消有效 CANCEL', excluded_count:'规则排除 EXCLUDED',delta_original:'N − 范围外 − 选中',delta_selected:'选中 − SALE − CANCEL − EXCLUDED',product_count:'商品全部',non_product_count:'非商品全部',invalid_count:'无效行（与类别交叉，不可再次相加）'};
    $('totals').replaceChildren();
    for (const [key,label] of Object.entries(labels)) {
      const p = document.createElement('p');
      p.textContent = label + '：' + data[key]; $('totals').append(p);
    }
    $('categories').replaceChildren();
    for (const [category,total] of Object.entries(data.categories)) {
      const tr = document.createElement('tr');
      for (const value of [category,total,...['SALE','CANCEL','EXCLUDED'].map(k => data.category_validity[category+'|'+k] || 0)]) {
        const td = document.createElement('td'); td.textContent = value; tr.append(td);
      }
      $('categories').append(tr);
    }
    $('coverage').textContent = JSON.stringify(data.cancel_coverage,null,2);
    $('quality').textContent = JSON.stringify({batch_id:data.batch_id,snapshot_id:data.snapshot_id,normalization_version:data.normalization_version,reason_counts:data.reason_counts,truncation:data.truncation,request_id:data.request_id},null,2);
  } catch(error) { $('status').textContent = '读取失败：' + error.message; }
}
$('load').addEventListener('click',load);
if ($('batch').value) load();
