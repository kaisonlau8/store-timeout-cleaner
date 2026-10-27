/* 离线 DOM 桩：运行真实 groups-list.js，验证导出与查询共用当前筛选。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/groups-list.js'), 'utf8');

function element() {
    return {
        value: '', href: '', innerHTML: '', textContent: '', children: [], listeners: {},
        classList: { toggle() {} }, setAttribute() {},
        appendChild(child) { this.children.push(child); },
        addEventListener(type, listener) {
            (this.listeners[type] || (this.listeners[type] = [])).push(listener);
        },
        emit(type) { (this.listeners[type] || []).forEach((listener) => listener({})); }
    };
}

function setup(filters, theaters = ['武汉特区', '华东战区']) {
    const ids = [
        'list-q', 'list-theater-select', 'list-store-q', 'list-robot-select',
        'groups-list-query', 'groups-list-export', 'groups-list-next', 'groups-list-prev',
        'groups-list-page-info', 'groups-list-error', 'groups-list-table-wrap', 'groups-list-empty'
    ];
    const elements = Object.fromEntries(ids.map((id) => [id, element()]));
    const requests = [];
    const window = {};
    const app = { elements, requests, window, fail: false };
    const context = {
        URLSearchParams, window,
        location: { pathname: '/groups', search: '?' + new URLSearchParams(filters).toString() },
        history: { replaceState() {} },
        document: {
            readyState: 'complete',
            getElementById: (id) => elements[id] || null,
            querySelectorAll: () => [],
            querySelector: () => element(),
            createElement: () => element()
        },
        // 不访问网络；所有查询只记录 URL 并返回本地响应。
        fetch: async (url) => {
            requests.push(new URL(url, 'http://localhost'));
            const params = requests[requests.length - 1].searchParams;
            return {
                ok: !app.fail,
                json: async () => ({
                    rows: [], total: 125, size: 50, page: Number(params.get('page')), theaters,
                    error: '测试查询失败'
                })
            };
        }
    };
    vm.runInNewContext(source, context);
    return app;
}

function exportFilters(app) {
    const url = new URL(app.elements['groups-list-export'].href, 'http://localhost');
    assert.equal(url.pathname, '/api/groups/list/export');
    assert.deepEqual([...url.searchParams.keys()].sort(), ['q', 'robot', 'store', 'theater']);
    return Object.fromEntries(url.searchParams);
}

function assertQueryMatchesExport(app, page) {
    const url = app.requests[app.requests.length - 1];
    assert.equal(url.pathname, '/api/groups/list');
    assert.equal(url.searchParams.get('page'), String(page));
    assert.equal(url.searchParams.get('size'), '50');
    const params = new URLSearchParams(url.searchParams);
    params.delete('page');
    params.delete('size');
    assert.deepEqual(Object.fromEntries(params), exportFilters(app));
}

async function settle() {
    for (let idx = 0; idx < 12; idx += 1) await Promise.resolve();
}

(async () => {
    const initial = { q: 'X700 & VIP', theater: '武汉特区', store: '武汉', robot: '0' };
    const app = setup({ ...initial, page: '3', size: '1', tab: 'list', keyword: '无关', only_robot: '1' });
    // 下拉框尚未加载时，也不能丢失 URL 中的战区或带入分页 / 关键字参数。
    assert.deepEqual(exportFilters(app), initial);
    await settle();
    assertQueryMatchesExport(app, 3);

    // 输入后未点查询，链接也应包含最新输入，且不会偷偷发起额外查询。
    const count = app.requests.length;
    app.elements['list-q'].value = '  新群 + ID  ';
    app.elements['list-q'].emit('input');
    app.elements['list-store-q'].value = '  上海  ';
    app.elements['list-store-q'].emit('change');
    assert.deepEqual(exportFilters(app), { ...initial, q: '新群 + ID', store: '上海' });
    assert.equal(app.requests.length, count);

    // 点击时再读取一次，避免自动填充等未触发输入事件的情况导出旧条件。
    app.elements['list-q'].value = '  R2  ';
    app.elements['groups-list-export'].emit('click');
    assert.equal(exportFilters(app).q, 'R2');
    assert.equal(app.requests.length, count);

    app.elements['list-theater-select'].value = '华东战区';
    app.elements['list-theater-select'].emit('change');
    await settle();
    app.elements['list-robot-select'].value = '1';
    app.elements['list-robot-select'].emit('change');
    await settle();
    assertQueryMatchesExport(app, 1);
    assert.deepEqual(exportFilters(app), { q: 'R2', theater: '华东战区', store: '上海', robot: '1' });

    app.elements['groups-list-next'].emit('click');
    await settle();
    assertQueryMatchesExport(app, 2);

    // 查询失败不应让导出链接退回上一次成功的筛选。
    app.fail = true;
    app.elements['list-q'].value = '失败查询';
    app.elements['groups-list-query'].emit('click');
    await settle();
    assertQueryMatchesExport(app, 1);
    assert.equal(exportFilters(app).q, '失败查询');

    // 当前 DB 已不包含 URL 中的战区时，保留该筛选，而不是误导出所有战区。
    const unknown = setup({ theater: '不存在战区', robot: '0' }, []);
    await settle();
    assert.equal(unknown.elements['list-theater-select'].value, '不存在战区');
    assert.equal(exportFilters(unknown).theater, '不存在战区');
    assertQueryMatchesExport(unknown, 1);
    console.log('ok');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
