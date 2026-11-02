'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const scripts = path.join(root, 'static', 'js');
const flush = async () => { for (let index = 0; index < 20; index += 1) await Promise.resolve(); };

function harness({ url = 'https://shop.test/', saved = {}, clipboardFailure = false } = {}) {
  const requests = [], mounted = [], unmounted = [], copied = [], toasts = [];
  const watchers = new Map(), timers = new Map();
  let timerId = 0, page;
  const storage = seed => {
    const values = new Map(Object.entries(seed || {}));
    return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) };
  };
  const ref = initial => {
    let value = initial;
    const target = {};
    Object.defineProperty(target, 'value', {
      get: () => value,
      set: next => {
        const previous = value;
        value = next;
        if (previous !== next && watchers.has(target)) Promise.resolve().then(() => watchers.get(target)(next, previous));
      },
    });
    return target;
  };
  const document = {
    documentElement: { scrollTop: 0, scrollHeight: 2000, clientHeight: 800 },
    body: { scrollTop: 0, scrollHeight: 2000, clientHeight: 800, appendChild() {}, removeChild() {} },
    addEventListener() {}, removeEventListener() {}, getElementById: () => null,
    createElement: () => ({ value: '', select() {} }), execCommand: () => true,
  };
  const context = {
    URL, URLSearchParams, document,
    location: { href: url, search: new URL(url).search },
    history: { length: 1, replaceState() {}, back() {} },
    localStorage: storage(saved), sessionStorage: storage(),
    pageYOffset: 0, innerHeight: 800, innerWidth: 375,
    addEventListener() {}, removeEventListener() {}, scrollTo() {},
    console: { warn() {}, error() {}, log() {} },
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: id => timers.delete(id), setInterval: () => ++timerId, clearInterval() {},
    navigator: { userAgent: 'mobile', clipboard: { writeText: async text => { if (clipboardFailure) throw new Error('denied'); copied.push(text); } } },
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
    Vue: {
      ref, computed: getter => ({ get value() { return getter(); } }),
      onMounted: callback => mounted.push(callback), onUnmounted: callback => unmounted.push(callback),
      watch: (target, callback) => watchers.set(target, callback),
      createApp: options => ({ mount() { page = options.setup(); } }),
    },
  };
  context.window = context;
  vm.createContext(context);
  const load = (...names) => names.forEach(name => vm.runInContext(fs.readFileSync(path.join(scripts, `${name}.js`), 'utf8'), context, { filename: `${name}.js` }));
  load('api', 'session', 'format', 'browser', 'feeds', 'account');
  const api = context.EasyShop.createApi();
  const session = context.EasyShop.createSessionStore();
  const makeFeeds = () => context.EasyShop.createFeeds({ ref, api, getUid: () => '', showToast: message => toasts.push(message) });
  const makeAccount = () => context.EasyShop.createAccount({ ref, api, session, showToast: message => toasts.push(message) });
  const finish = async (index, data, { status = 200 } = {}) => {
    assert.ok(requests[index], `Request ${index} exists`);
    requests[index].resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
    await flush();
  };
  return { context, api, session, requests, mounted, unmounted, copied, timers, toasts, load, makeFeeds, makeAccount, finish, get page() { return page; } };
}
const goodsResponse = (id, hasMore = true) => ({ status: 'success', data: [{ goods_id: id, platform: 'pdd' }], has_more: hasMore });
const user = (uid = 'A', token = 'token-A') => ({ uid, phone: '13800000000', nickname: uid, auth_token: token });

test('transport preserves HTTP errors and carries JSON and Bearer credentials', async () => {
  const h = harness();
  const pending = h.api.post('/example', { value: 1 }, { token: 'secret' });
  assert.equal(h.requests[0].options.headers.Authorization, 'Bearer secret');
  assert.equal(h.requests[0].options.headers['Content-Type'], 'application/json');
  assert.equal(h.requests[0].options.body, '{"value":1}');
  await h.finish(0, { detail: 'bad request' }, { status: 422 });
  const result = await pending;
  assert.equal(result.ok, false);
  assert.equal(result.status, 422);
  assert.equal(result.data.detail, 'bad request');
});

test('format and product identity helpers reject empty-key and cross-platform cache matches', () => {
  const { context: { EasyShop: shop } } = harness();
  assert.equal(shop.formatPhone('13800000000'), '138****0000');
  assert.equal(shop.formatMoney('1.2'), '1.20');
  assert.equal(shop.formatMoney('bad'), '0.00');
  assert.equal(shop.matchesGoodsSummary({ goods_id: 'A', goods_sign: '' }, { goods_id: 'B', goods_sign: '' }), false);
  assert.equal(shop.matchesGoodsSummary({ goods_id: 'A', platform: 'pdd' }, { goods_id: 'A', platform: 'jd' }), false);
  assert.equal(shop.matchesGoodsSummary({ product_id: 'A', platform: 'pdd' }, { goods_id: 'A', platform: 'pdd' }), true);
});

test('home reset fetches the new channel during loading and ignores the older response', async () => {
  const h = harness(), feed = h.makeFeeds();
  const old = feed.loadHomeGoods(true);
  const latest = feed.switchChannel(1);
  assert.equal(h.requests.length, 2);
  assert.equal(new URL(h.requests[1].url, 'https://shop.test').searchParams.get('channel_type'), '1');
  await h.finish(1, goodsResponse('channel-1'));
  await latest;
  await h.finish(0, goodsResponse('channel-5'));
  await old;
  assert.equal(feed.currentChannel.value, 1);
  assert.equal(feed.homeGoodsList.value[0].goods_id, 'channel-1');
  const uncached = feed.switchChannel(5);
  assert.equal(h.requests.length, 3, 'stale response did not populate channel 5 cache');
  await h.finish(2, goodsResponse('fresh-5'));
  await uncached;
});

test('cached channel switching also invalidates an in-flight page append', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadHomeGoods(true); await h.finish(0, goodsResponse('A'));
  feed.switchChannel(1); await h.finish(1, goodsResponse('B'));
  feed.switchChannel(5);
  const oldPage = feed.loadHomeGoods(false);
  feed.switchChannel(1);
  assert.equal(feed.homeLoading.value, false);
  await h.finish(2, goodsResponse('late-A2'));
  await oldPage;
  assert.equal(feed.homeGoodsList.value.length, 1);
  assert.equal(feed.homeGoodsList.value[0].goods_id, 'B');
  feed.switchChannel(5);
  assert.equal(feed.homeGoodsList.value.length, 1);
});

test('a stale completion cannot hide the current loading state; append requests are deduplicated', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadHomeGoods(true);
  feed.switchChannel(1);
  await h.finish(0, goodsResponse('old'));
  assert.equal(feed.homeLoading.value, true);
  await feed.loadHomeGoods(false);
  assert.equal(h.requests.length, 2);
  await h.finish(1, goodsResponse('new'));
  assert.equal(feed.homeLoading.value, false);
});

test('search commits a separate query so draft edits cannot corrupt pagination or snapshots', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.homeSearchKeyword.value = 'A'; feed.onSearchHome();
  await h.finish(0, goodsResponse('A1'));
  feed.homeSearchKeyword.value = 'unsubmitted B'; feed.loadHomeGoods(false);
  const query = new URL(h.requests[1].url, 'https://shop.test').searchParams;
  assert.equal(query.get('keyword'), 'A');
  assert.equal(query.get('page'), '2');
  await h.finish(1, goodsResponse('A2'));
  feed.saveSearchState();
  const saved = JSON.parse(h.context.sessionStorage.getItem('easy_shop_home_search_state'));
  assert.equal(saved.keyword, 'A');
  assert.equal(saved.list.length, 2);
  feed.onSearchHome();
  assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('page'), '1');
  await h.finish(2, goodsResponse('B1'));
});

test('business-error pagination retry requests the failed offset instead of ending the feed', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadHomeGoods(true); await h.finish(0, goodsResponse('A'));
  feed.loadHomeGoods(false); await h.finish(1, { status: 'error' });
  assert.equal(feed.homeError.value, true);
  const retry = feed.retryLoadHomeGoods();
  assert.equal(h.requests.length, 3);
  assert.equal(h.requests[2].url, h.requests[1].url);
  await h.finish(2, goodsResponse('A2', false)); await retry;
  assert.equal(feed.homeGoodsList.value.length, 2);
  await feed.loadHomeGoods(false);
  assert.equal(h.requests.length, 3);
});

test('returning from detail restores the committed list, page and terminal state', () => {
  const h = harness(), feed = h.makeFeeds();
  h.context.sessionStorage.setItem('easy_shop_home_search_state', JSON.stringify({ keyword: 'A', list: [{ goods_id: 'A' }], page: 3, offset: 40, hasMore: false, scrollTop: 100 }));
  feed.initializeHome(new URLSearchParams('keyword=A'));
  assert.equal(h.requests.length, 0);
  assert.equal(feed.homeSearchKeyword.value, 'A');
  assert.equal(feed.homeHasMore.value, false);
});

test('boom platform and sort resets supersede pending requests', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadBoomGoods(true);
  feed.switchBoomPlatform('pdd');
  feed.switchBoomSort('price_asc');
  assert.equal(h.requests.length, 3);
  await h.finish(2, goodsResponse('latest'));
  await h.finish(1, goodsResponse('older'));
  await h.finish(0, goodsResponse('oldest'));
  assert.equal(feed.boomGoodsList.value[0].goods_id, 'latest');
  const query = new URL(h.requests[2].url, 'https://shop.test').searchParams;
  assert.equal(query.get('platform'), 'pdd');
  assert.equal(query.get('sort_by'), 'price_asc');
  assert.equal(query.get('page'), '1');
});

test('platform metadata keeps unavailable platforms as non-querying placeholders', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadPlatforms();
  await h.finish(0, { status: 'success', data: [{ code: 'pdd', name: '拼多多', active: true }, { code: 'taobao', name: '淘宝', active: false }, { code: 'jd', name: '京东', active: false }] });
  feed.switchBoomPlatform('jd');
  assert.equal(h.requests.length, 1);
  assert.equal(feed.boomPlatform.value, 'all');
  assert.equal(h.toasts.length, 1);
});

test('concurrent session restores use one request and logout invalidates its response', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } });
  const first = h.session.restore(h.api), second = h.session.restore(h.api);
  assert.equal(first, second);
  assert.equal(h.requests.length, 1);
  h.session.clear();
  await h.finish(0, { status: 'success', user: user() });
  assert.equal((await first).stale, true);
  assert.equal(h.context.localStorage.getItem('easy_shop_uid'), null);
});

test('logout prevents old profile and order responses from restoring private state', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  account.isLoggedIn.value = true; account.uid.value = 'A';
  const profile = account.fetchUserInfo(), orders = account.syncAndQueryOrders();
  account.handleLogout();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { order_list: [{ order_sn: 'A-order' }] } });
  await Promise.all([profile, orders]);
  assert.equal(account.isLoggedIn.value, false);
  assert.equal(account.uid.value, '');
  assert.equal(account.userOrdersData.value.order_list.length, 0);
  assert.equal(h.context.localStorage.getItem('easy_shop_token'), null);
});

test('the latest wallet date filter wins when responses arrive backwards', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  account.isLoggedIn.value = true; account.uid.value = 'A';
  account.switchOrderDays(1); account.switchOrderDays(30);
  await h.finish(1, { status: 'success', data: { period: 30, order_list: [] } });
  await h.finish(0, { status: 'success', data: { period: 1, order_list: [] } });
  assert.equal(account.orderDays.value, 30);
  assert.equal(account.userOrdersData.value.period, 30);
  assert.equal(account.isSyncingOrders.value, false);
});

test('a newer login prevents an old restore from replacing the account', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  account.fetchUserInfo();
  account.formPhone.value = '13800000000'; account.formPassword.value = 'password';
  const login = account.handleRegisterOrLogin();
  await h.finish(1, { status: 'success', user: user('B', 'token-B') }); await login;
  await h.finish(0, { status: 'success', user: user('A', 'token-A') });
  assert.equal(account.uid.value, 'B');
  assert.equal(h.context.localStorage.getItem('easy_shop_token'), 'token-B');
  assert.equal(h.requests[2].options.headers.Authorization, 'Bearer token-B');
  await h.finish(2, { status: 'success', data: { order_list: [] } });
});

test('opening the wallet with a saved session queries orders after authentication settles', async () => {
  const h = harness({ url: 'https://shop.test/?tab=user', saved: { easy_shop_token: 'token-A' } });
  h.load('app'); h.mounted[0](); await flush();
  assert.equal(h.requests.filter(request => request.url === '/api/user/info').length, 1);
  const infoIndex = h.requests.findIndex(request => request.url === '/api/user/info');
  assert.equal(h.requests.filter(request => request.url.startsWith('/api/order/')).length, 0);
  await h.finish(infoIndex, { status: 'success', user: user() });
  const orderIndex = h.requests.findIndex(request => request.url.startsWith('/api/order/'));
  assert.ok(orderIndex >= 0);
  assert.equal(h.requests[orderIndex].options.headers.Authorization, 'Bearer token-A');
  await h.finish(orderIndex, { status: 'success', data: { order_list: [{ order_sn: 'restored-order' }] } });
  assert.equal(h.page.userOrdersData.value.order_list[0].order_sn, 'restored-order');
});

test('homepage card navigation preserves both product keys and the selected platform', () => {
  const h = harness(); h.load('app');
  h.page.openGoodsDetail({ goods_id: '123', goods_sign: 'signed', platform: 'pdd', name: 'A' });
  const query = new URL(h.context.location.href, 'https://shop.test').searchParams;
  assert.equal(query.get('goods_id'), '123');
  assert.equal(query.get('goods_sign'), 'signed');
  assert.equal(query.get('platform'), 'pdd');
  assert.equal(JSON.parse(h.context.sessionStorage.getItem('current_goods_summary')).name, 'A');
});

test('detail rejects another product cache and a saved phone alone is not a session', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=B&goods_sign=&platform=pdd&uid=untrusted', saved: { easy_shop_phone: '13800000000', easy_shop_uid: 'old' } });
  h.context.sessionStorage.setItem('current_goods_summary', JSON.stringify({ goods_id: 'A', goods_sign: '', platform: 'pdd' }));
  h.load('detail'); const init = h.mounted[0](); await flush();
  assert.equal(h.page.goods.value, null);
  assert.equal(h.page.isLoggedIn.value, false);
  assert.equal(h.page.uid.value, '');
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests.filter(request => request.options.method === 'POST').length, 0);
  await h.finish(0, { status: 'success', data: { goods_id: 'B', platform: 'pdd', name: 'B' } }); await init;
  assert.equal(h.page.goods.value.goods_id, 'B');
});

test('detail uses verified UID, converts only on demand and deduplicates concurrent actions', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A&uid=untrusted', saved: { easy_shop_token: 'token-A', easy_shop_phone: '13800000000', easy_shop_uid: 'A' } });
  h.load('detail'); const init = h.mounted[0]();
  const info = h.requests.findIndex(request => request.url === '/api/user/info');
  const detail = h.requests.findIndex(request => request.url.startsWith('/api/home/detail'));
  await h.finish(info, { status: 'success', user: user() });
  await h.finish(detail, { status: 'success', data: { goods_id: 'A', goods_sign: 'sign-A', platform: 'pdd', name: 'A', price: 10 } }); await init;
  assert.equal(h.page.uid.value, 'A');
  assert.equal(h.requests.filter(request => request.options.method === 'POST').length, 0);
  const first = h.page.handleCopyPromoteLink(), second = h.page.handleCopyPromoteLink(); await flush();
  const conversions = h.requests.filter(request => request.url === '/api/promote/convert');
  assert.equal(conversions.length, 1);
  assert.equal(JSON.parse(conversions[0].options.body).uid, 'A');
  assert.equal(JSON.parse(conversions[0].options.body).goods_sign, 'sign-A');
  await h.finish(h.requests.indexOf(conversions[0]), { status: 'success', final_link: 'https://buy.test/A', command_text: 'command-A' });
  await Promise.all([first, second]);
  await h.page.handleCopyPromoteLink();
  assert.equal(h.requests.filter(request => request.url === '/api/promote/convert').length, 1);
  assert.equal(h.copied[0], 'command-A');
});

test('detail fallback links are usable but not cached as a successful activation', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A' });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', data: { goods_id: 'A', platform: 'pdd', name: 'A', product_url: 'https://goods.test/A' } }); await init;
  const first = h.page.handleCopyPromoteLink(); await flush();
  await h.finish(1, { status: 'success', is_fallback: true, final_link: 'https://goods.test/A' }); await first;
  const second = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests.length, 3);
  assert.equal(JSON.parse(h.requests[2].options.body).uid, null);
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/A' }); await second;
});

test('detail retries retain the summary and ignore superseded detail responses', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A' });
  h.context.sessionStorage.setItem('current_goods_summary', JSON.stringify({ goods_id: 'A', platform: 'pdd', name: 'summary' }));
  h.load('detail'); const init = h.mounted[0]();
  assert.equal(h.page.goods.value.name, 'summary');
  await h.finish(0, { status: 'error' }); await init;
  assert.equal(h.page.goods.value.name, 'summary');
  h.page.retryLoadDeepDetail(); h.page.retryLoadDeepDetail();
  await h.finish(2, { status: 'success', data: { goods_id: 'A', name: 'latest', carousel_images: ['one', 'two'] } });
  await h.finish(1, { status: 'success', data: { goods_id: 'A', name: 'stale' } });
  assert.equal(h.page.goods.value.name, 'latest');
  assert.equal(h.page.carouselList.value.length, 2);
  assert.equal(h.page.detailError.value, false);
});

test('clipboard denial falls back and mobile purchase keeps the web fallback', async () => {
  const h = harness({ clipboardFailure: true });
  const browser = h.context.EasyShop.createBrowserTools({ ref: h.context.Vue.ref });
  await browser.copyText('text');
  assert.equal(browser.toastMessage.value, '已复制到剪贴板');
  browser.jumpToLink({ schema_url: 'pdd://goods/A', final_link: 'https://buy.test/A' });
  const initial = [...h.timers.values()].find(timer => timer.delay === 400); initial.callback();
  assert.equal(h.context.location.href, 'pdd://goods/A');
  const fallback = [...h.timers.values()].find(timer => timer.delay === 800); fallback.callback();
  assert.equal(h.context.location.href, 'https://buy.test/A');
  browser.dispose();
});

test('sign-only goods retain distinct platform-qualified keys and deduplicate without changing paging offsets', async () => {
  const h = harness(), feed = h.makeFeeds(), shop = h.context.EasyShop;
  const first = { goods_id: '', product_id: '', goods_sign: 'sign-A', platform: 'pdd' };
  const second = { goods_id: '', product_id: '', goods_sign: 'sign-B', platform: 'pdd' };
  assert.notEqual(shop.goodsKey(first), shop.goodsKey(second));
  assert.notEqual(shop.goodsKey(first), shop.goodsKey({ ...first, platform: 'jd' }));
  assert.equal(shop.matchesGoodsSummary(first, { goods_sign: 'sign-A', platform: 'pdd' }), true);
  assert.equal(shop.matchesGoodsSummary(first, { goods_id: '', goods_sign: '', platform: 'pdd' }), false);
  feed.loadHomeGoods(true);
  await h.finish(0, { status: 'success', data: [first, second], has_more: true });
  feed.loadHomeGoods(false);
  await h.finish(1, { status: 'success', data: [first, { goods_sign: 'sign-C', platform: 'pdd' }], has_more: true });
  assert.equal(feed.homeGoodsList.value.length, 3);
  feed.loadHomeGoods(false);
  assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('offset'), '4');
  await h.finish(2, { status: 'success', data: [], has_more: false });
  const source = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  assert.equal((source.match(/:key="goodsKey\(item\)"/g) || []).length, 2);
});

test('a removed detail session cannot copy or reuse an old personalized conversion', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A', saved: { easy_shop_token: 'token-A' } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { goods_id: 'A', platform: 'pdd', name: 'A' } }); await init;
  const personalized = h.page.handleCopyPromoteLink(); await flush();
  h.context.localStorage.removeItem('easy_shop_token');
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/personal-A', command_text: 'private-A' });
  await personalized;
  assert.equal(h.copied.length, 0);
  const anonymous = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(JSON.parse(h.requests[3].options.body).uid, null);
  await h.finish(3, { status: 'success', final_link: 'https://buy.test/guest' }); await anonymous;
  assert.equal(h.page.isLoggedIn.value, false);
});

test('homepage shared script dependencies exist and toast is outside the authorization backdrop', () => {
  const source = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const dependencies = [...source.matchAll(/<script src="\/static\/js\/([^"]+)"/g)].map(match => match[1]);
  for (const name of dependencies) assert.equal(fs.existsSync(path.join(scripts, name)), true, name);
  assert.ok(dependencies.indexOf('api.js') < dependencies.indexOf('app.js'));
  assert.ok(dependencies.indexOf('account.js') < dependencies.indexOf('app.js'));
  const backdrop = source.indexOf('class="modal-backdrop"');
  const loader = source.indexOf('class="smart-loader-overlay"');
  const between = source.slice(backdrop, loader);
  const divStarts = (between.match(/<div\b/g) || []).length;
  const divEnds = (between.match(/<\/div>/g) || []).length;
  assert.equal(divStarts, divEnds, 'authorization backdrop is closed before the global loader and toast');
});

test('detail HTML loads shared dependencies and the external controller exactly once', () => {
  const source = fs.readFileSync(path.join(root, 'static', 'detail.html'), 'utf8');
  const dependencies = [...source.matchAll(/<script src="\/static\/js\/([^"]+)"/g)].map(match => match[1]);
  for (const name of dependencies) assert.equal(fs.existsSync(path.join(scripts, name)), true, name);
  assert.equal(dependencies.filter(name => name === 'detail.js').length, 1);
  assert.ok(dependencies.indexOf('session.js') < dependencies.indexOf('detail.js'));
  assert.ok(dependencies.indexOf('browser.js') < dependencies.indexOf('detail.js'));
  assert.equal(source.includes('const { createApp, ref, computed, onMounted } = Vue;'), false);
  assert.ok(source.includes('v-for="(guarantee, idx) in goods.service_guarantees"'));
});

test('a transient profile server failure does not erase a valid saved token', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } });
  const pending = h.session.restore(h.api);
  await h.finish(0, { error_msg: 'database unavailable' }, { status: 503 });
  assert.equal((await pending).unavailable, true);
  assert.equal(h.session.token(), 'token-A');
  const retry = h.session.restore(h.api);
  await h.finish(1, { detail: 'expired' }, { status: 401 });
  assert.equal((await retry).invalid, true);
  assert.equal(h.session.token(), '');
});

test('platform metadata can add a test platform without changing the list controller', async () => {
  const h = harness(), feed = h.makeFeeds();
  const loading = feed.loadPlatforms();
  await h.finish(0, { status: 'success', data: [
    { code: 'pdd', name: '拼多多', active: true },
    { code: 'fixture', name: '测试替身', active: true },
  ] });
  await loading;
  assert.equal(feed.boomPlatforms.value.find(item => item.code === 'fixture').active, true);
  const query = feed.switchBoomPlatform('fixture');
  assert.equal(new URL(h.requests[1].url, 'https://shop.test').searchParams.get('platform'), 'fixture');
  await h.finish(1, { status: 'success', data: [], has_more: false });
  await query;
});

test('manual wallet refresh does not claim an unfinished background sync succeeded', async () => {
  const h = harness(), account = h.makeAccount();
  h.session.saveUser(user());
  account.isLoggedIn.value = true;
  account.uid.value = 'A';
  const query = account.syncAndQueryOrders(true);
  await h.finish(0, { status: 'success', data: { sync_status: 'running', order_list: [] } });
  await query;
  assert.equal(h.toasts.at(-1), '已刷新本地订单，后台同步进行中');
});

test('summary identity prioritizes a supplied Sign and preserves known zero coupon values', () => {
  const shop = harness().context.EasyShop;
  assert.equal(shop.matchesGoodsSummary(
    { goods_id: 'B', goods_sign: 'SIGN_B' }, { goods_id: 'B', goods_sign: 'SIGN_A' }), false);
  assert.equal(shop.goodsIdentity({ goods_id: '0', goods_sign: 'SIGN_A' }).goods_id, '');
  assert.notEqual(shop.goodsKey({ goods_id: 'A' }), shop.goodsKey({ goods_sign: 'A' }));
  assert.equal(shop.promotionPayload({ price: 0, coupon_amount: 0, saved_price: 10 }, '').coupon_amount, 0);
});
