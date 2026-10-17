/* Executed with Node's built-in test runner; no browser/Edge acceptance claim. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');

function environment() {
  const nodes = new Map();
  class Node {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.value = ''; this.textContent = ''; this.disabled = false; this.hidden = false; this.dataset = {}; }
    append(node) { this.children.push(node); node.parentNode = this; }
    replaceChildren() { this.children = []; }
    replaceWith(node) { node.parentNode = this.parentNode; this.parentNode.children = this.parentNode.children.map(c => c === this ? node : c); }
    querySelector() { return this.children.find(n => n.tag === 'button'); }
    addEventListener(event, handler) { this[event] = handler; }
    get text() { return [this.textContent, ...this.children.map(c => c.text)].join(' '); }
  }
  const shell = new Node();
  for (const id of ['detail', 'message', 'difference-id', 'actions', 'assign', 'handler', 'start', 'close', 'note', 'detail-form', 'items', 'run-id', 'filter-form', 'status', 'type', 'assignee', 'assignee-label', 'prev', 'next', 'page', 'export', 'summary', 'result', 'run-form', 'export-link', 'counts-link', 'differences-link']) {
    const n = new Node(id === 'handler' ? 'select' : 'div'); nodes.set(id, n); const parent = new Node(); shell.append(parent); parent.append(n);
  }
  const context = vm.createContext({window: {}, document: {getElementById: id => nodes.get(id), createElement: tag => new Node(tag), cookie: '', querySelector: () => shell}, history: {replaceState() {}}, location: {pathname: '/detail', search: '?difference_id=d', assign() {}}, URLSearchParams, Headers, setTimeout, console});
  nodes.get('filter-form').append(new Node('button'));
  vm.runInContext(fs.readFileSync(path.join(root, '主程序/web/static/js/api.js'), 'utf8'), context);
  context.appApi = context.window.appApi;
  return {context, api: context.appApi, nodes};
}

test('lossless integer tokens: 2^53 neighborhood, positive and negative INT64 endpoints', () => {
  const {api} = environment();
  const values = ['9007199254740990', '9007199254740991', '9007199254740992', '9007199254740993', '9223372036854775806', '9223372036854775807', '9223372036854775808'];
  for (const magnitude of values) for (const sign of ['', '-']) {
    const token = sign + magnitude, value = api.parseJSON(`{"n":${token}}`).n;
    assert.equal(BigInt(value), BigInt(token), token);
    assert.equal(typeof value, BigInt(magnitude) > 9007199254740991n ? 'string' : 'number');
    assert.doesNotThrow(() => JSON.stringify({n: value}));
  }
});

test('string tokens, field names, escaped quotes and backslashes never rewritten', () => {
  const {api} = environment();
  const object = {'9223372036854775807': '9223372036854775807', text: '"amount_units":9223372036854775807', escape: 'slash\\ and quote" then 9007199254740993', unicode: '中文'};
  assert.deepEqual(JSON.parse(JSON.stringify(api.parseJSON(JSON.stringify(object)))), object);
  assert.equal(api.parseJSON('"\\u0039\\u0030\\u0030"'), '900');
});

test('safe integers, decimals and exponents retain native Number behavior; malformed JSON throws', () => {
  const {api} = environment();
  for (const text of ['0', '-0', '42', '1.25', '-0.0049', '1e3', '-1.5E-3', '9007199254740993.0', '9.223372036854776e18', 'true', 'null']) {
    assert.ok(Object.is(api.parseJSON(text), JSON.parse(text)), text);
  }
  for (const text of ['', '01', '-', '1.', '1e', 'NaN', 'Infinity', '[1,]', '{"n":1,}', '{"n":1 "x":2}', '"unterminated', '"bad\\q"', '{unquoted:1}']) {
    assert.throws(() => api.parseJSON(text), {name: 'SyntaxError'}, text);
  }
});

test('money FR07 ROUND_HALF_UP, exact4 and original sign including negative zero', () => {
  const {api} = environment();
  const cases = [[0, '£0.00（精确 £0.0000，0 units）'], [49, '£0.00（精确 £0.0049，49 units）'], [-49, '-£0.00（精确 £-0.0049，-49 units）'], [50, '£0.01（精确 £0.0050，50 units）'], [-50, '-£0.01（精确 £-0.0050，-50 units）'], [51, '£0.01（精确 £0.0051，51 units）'], [-51, '-£0.01（精确 £-0.0051，-51 units）'], [10050, '£1.01（精确 £1.0050，10050 units）'], [-10050, '-£1.01（精确 £-1.0050，-10050 units）'], ['9223372036854775807', '£922337203685477.58（精确 £922337203685477.5807，9223372036854775807 units）']];
  cases.forEach(([units, expected]) => assert.equal(api.money(units), expected));
  assert.throws(() => api.money(JSON.parse('9223372036854775807')), /不能恢复已舍入Number/);
});

test('request uses lossless text parser for success and error JSON', async () => {
  const {context, api} = environment();
  context.fetch = async () => ({ok: true, status: 200, headers: new Headers({'Content-Type': 'application/json'}), text: async () => '{"amount_units":9223372036854775807}'});
  assert.equal((await api.request('/test')).data.amount_units, '9223372036854775807');
  context.fetch = async () => ({ok: false, status: 409, headers: new Headers(), text: async () => '{"error":{"message":"冲突","details":{"n":-9223372036854775808}},"request_id":"original"}'});
  await assert.rejects(api.request('/test'), e => e.status === 409 && e.payload.error.details.n === '-9223372036854775808' && e.payload.request_id === 'original');
  context.fetch = async () => ({ok: true, status: 200, headers: new Headers({'Content-Type': 'application/json'}), text: async () => '{'});
  await assert.rejects(api.request('/test'), {name: 'SyntaxError'});
});

test('detail 409 refreshes ownership/version/status; preserves note and original request ID; no retry', async () => {
  const {context, api, nodes} = environment();
  let reads = 0, writes = 0;
  const payload = version => ({id: 'd', run_id: 'r', type: 'INSUFFICIENT_QUANTITY', work_status: version === 1 ? 'PENDING' : 'CLOSED', version, assignee_id: 'me', quantity: '1', amount_units: '20000', flags: [], candidates: [], source: {sheet: 'synthetic', source_row: 2, invoice: '<script>bad</script>', stock: 'SKU', customer: '123', country: 'UK', price_units: '20000', amount_units: '-20000', flags: ['TRUNCATED_INVOICE_RANGE'], quality: {provided: true, quality_version: 'v2'}}, history: []});
  api.me = async () => ({id: 'me', role: 'HANDLER'});
  api.request = async (url, options) => {
    if (!options) return {data: payload(++reads)};
    writes++;
    const e = new Error('旧版本'); e.status = 409; e.payload = {error: {code: 'VERSION_CONFLICT'}, request_id: 'original-conflict-id'}; throw e;
  };
  await vm.runInContext(fs.readFileSync(path.join(root, '主程序/web/static/js/detail.js'), 'utf8'), context);
  assert.equal(nodes.get('start').disabled, false);
  assert.equal(nodes.get('close').disabled, true);
  assert.match(nodes.get('detail').text, /来源行/);
  assert.match(nodes.get('detail').text, /TRUNCATED_INVOICE_RANGE/);
  assert.match(nodes.get('detail').text, /技术详情/);
  nodes.get('note').value = '保留中的处理说明';
  await nodes.get('start').onclick();
  assert.equal(reads, 2); assert.equal(writes, 1);
  assert.equal(nodes.get('note').value, '保留中的处理说明');
  assert.equal(nodes.get('start').disabled, true); assert.equal(nodes.get('close').disabled, true);
  assert.match(nodes.get('message').text, /original-conflict-id/);
  assert.match(nodes.get('message').text, /未自动重试/);
  assert.doesNotMatch(nodes.get('message').text, /处理中，请稍候/);
  assert.match(nodes.get('detail').text, /版本：2/);
});

test('differences type/status/assignee, bookmark, reset page and export share filters', async () => {
  const {context, api, nodes} = environment();
  context.location.search = '?run_id=r&type=PRICE_MISMATCH&status=PENDING&assignee_id=h&page=3';
  api.me = async () => ({id: 's', role: 'SUPERVISOR'});
  api.runPicker = async () => {};
  const requests = [], bookmarks = [], downloads = [];
  context.history.replaceState = (_, __, url) => bookmarks.push(url);
  api.download = async (run, filters) => downloads.push({run, filters});
  api.request = async url => {
    if (url === '/users/handlers') return {data: {items: [{id: 'h', name: '合成处理人'}]}};
    requests.push(url);
    return {data: {items: [{id: 'd', type: 'PRICE_MISMATCH', work_status: 'PENDING', match_status: 'UNMATCHED', assignee_id: 'h', version: 2, quantity: '1', amount_units: '9223372036854775807'}], total: 80}};
  };
  await vm.runInContext(fs.readFileSync(path.join(root, '主程序/web/static/js/differences.js'), 'utf8'), context);
  let query = new URLSearchParams(requests[0].split('?')[1]);
  assert.equal(query.get('page'), '3'); assert.equal(query.get('type'), 'PRICE_MISMATCH');
  assert.equal(query.get('status'), 'PENDING'); assert.equal(query.get('assignee_id'), 'h');
  assert.match(nodes.get('items').text, /单价不一致/);
  const link = nodes.get('items').children[0].children[1].children[0].children[5].children[0];
  assert.match(link.href, /page=3/); assert.match(link.href, /difference_id=d/);
  await nodes.get('export').onclick();
  assert.equal(downloads[0].run, 'r'); assert.deepEqual(JSON.parse(JSON.stringify(downloads[0].filters)), {type: 'PRICE_MISMATCH', status: 'PENDING', assignee_id: 'h'});
  nodes.get('status').value = 'CLOSED'; await nodes.get('status').onchange();
  query = new URLSearchParams(requests[1].split('?')[1]);
  assert.equal(query.get('page'), '1'); assert.equal(query.get('status'), 'CLOSED');
  assert.equal(nodes.get('prev').disabled, true);
  assert.match(bookmarks.at(-1), /status=CLOSED/);
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

for (const oldOutcome of ['success', 'failure']) test(`differences latest snapshot wins when initial ${oldOutcome} arrives after new filter`, async () => {
  const {context, api, nodes} = environment();
  context.location.search = '?run_id=old-run&page=3';
  api.me = async () => ({id: 's', role: 'SUPERVISOR'});
  api.runPicker = async () => {};
  const pending = [], requested = [], bookmarks = [], downloads = [], started = deferred();
  context.history.replaceState = (_, __, url) => bookmarks.push(url);
  api.download = async (run, filters) => downloads.push({run, filters});
  api.request = async url => {
    if (url === '/users/handlers') return {data: {items: []}};
    requested.push(url); const response = deferred(); pending.push(response); started.resolve(); return response.promise;
  };
  const initial = vm.runInContext(fs.readFileSync(path.join(root, '主程序/web/static/js/differences.js'), 'utf8'), context);
  await started.promise; // The actual initial load is now suspended in request().
  nodes.get('run-id').value = 'new-run';
  nodes.get('type').value = 'NON_PRODUCT_REVIEW'; nodes.get('status').value = 'PENDING';
  const latest = nodes.get('type').onchange();
  assert.equal(pending.length, 2);
  const latestQuery = new URLSearchParams(requested[1].split('?')[1]);
  assert.equal(latestQuery.get('page'), '1'); assert.equal(latestQuery.get('type'), 'NON_PRODUCT_REVIEW');
  const item = (id, type) => ({id, type, work_status: 'PENDING', match_status: 'UNMATCHED', assignee_id: null, version: 0, quantity: '1', amount_units: '10099'});
  pending[1].resolve({data: {items: [item('new-only', 'NON_PRODUCT_REVIEW')], total: 1}});
  await latest;
  const state = () => ({text: nodes.get('items').text, bookmarks: [...bookmarks], page: nodes.get('page').text,
    message: nodes.get('message').text, prev: nodes.get('prev').disabled, next: nodes.get('next').disabled,
    query: nodes.get('filter-form').querySelector('button').disabled});
  const committed = state();
  assert.match(committed.text, /非商品人工复核/); assert.doesNotMatch(committed.text, /数量不足/);
  assert.equal(committed.page, '第 1 页 / 1 条'); assert.equal(committed.prev, true); assert.equal(committed.next, true); assert.equal(committed.query, false);
  assert.equal(bookmarks.length, 1);
  const bookmark = new URLSearchParams(bookmarks[0].split('?')[1]);
  assert.equal(bookmark.get('run_id'), 'new-run'); assert.equal(bookmark.get('type'), 'NON_PRODUCT_REVIEW'); assert.equal(bookmark.get('page'), '1');
  const body = nodes.get('items').children[0].children[1]; assert.equal(body.children.length, 1);
  const link = body.children[0].children[5].children[0];
  const href = new URLSearchParams(link.href.split('?')[1]);
  assert.equal(href.get('run_id'), 'new-run'); assert.equal(href.get('type'), 'NON_PRODUCT_REVIEW'); assert.equal(href.get('difference_id'), 'new-only');
  if (oldOutcome === 'success') pending[0].resolve({data: {items: [item('old-fee', 'NON_PRODUCT_REVIEW'), item('old-product', 'INSUFFICIENT_QUANTITY')], total: 80}});
  else { const error = new Error('stale response failure'); error.status = 503; pending[0].reject(error); }
  await initial;
  assert.deepEqual(state(), committed); // Includes URL, pager, status and query button.
  assert.equal(body.children.length, 1);
  await nodes.get('export').onclick();
  assert.equal(downloads[0].run, 'new-run');
  assert.deepEqual(JSON.parse(JSON.stringify(downloads[0].filters)), {type: 'NON_PRODUCT_REVIEW', status: 'PENDING'});
});

test('results export failure is awaited, shown with request ID and restores button after busy state', async () => {
  const {context, api, nodes} = environment();
  context.location.search = '?run_id=r';
  api.me = async () => ({id: 'f', role: 'FINANCE'});
  api.request = async () => ({data: {status: 'COMPLETED', attempt: 1, batch_id: 'b', matched: 2, suspected: 1,
    no_original: 0, non_product_review: 1, difference_count: 2, cancel_amount_units: '50099', allocated_amount_units: '40000', unmatched_amount_units: '10099', amount_delta_units: '0', quality: {provided: false}}});
  const failure = deferred(), downloads = [];
  api.download = async run => { downloads.push(run); api.status('生成授权CSV并审计中…'); await failure.promise; };
  await vm.runInContext(fs.readFileSync(path.join(root, '主程序/web/static/js/results.js'), 'utf8'), context);
  const button = nodes.get('export-link');
  assert.equal(button.hidden, false);
  const operation = button.onclick();
  assert.equal(button.disabled, true); assert.deepEqual(downloads, ['r']);
  const error = new Error('导出失败'); error.status = 503; error.payload = {error: {code: 'EXPORT_FAILED'}, request_id: 'export-failure-request'};
  failure.reject(error);
  await operation;
  assert.equal(button.disabled, false);
  assert.match(nodes.get('message').text, /EXPORT_FAILED/); assert.match(nodes.get('message').text, /export-failure-request/);
  assert.doesNotMatch(nodes.get('message').text, /生成授权CSV并审计中|处理中，请稍候/);
});

test('all business JavaScript parses with the actual Node engine', () => {
  const directory = path.join(root, '主程序/web/static/js');
  for (const name of fs.readdirSync(directory).filter(n => n.endsWith('.js'))) assert.doesNotThrow(() => new vm.Script(fs.readFileSync(path.join(directory, name), 'utf8'), {filename: name}));
});
