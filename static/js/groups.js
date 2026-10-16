document.addEventListener('DOMContentLoaded', () => {
    const DEFAULT_KEYWORD = 'X700-VIP';
    const keywordInput = document.getElementById('keyword-input');
    const theaterSelect = document.getElementById('theater-select');
    const storeQuery = document.getElementById('store-q');
    const robotOnly = document.getElementById('robot-only');
    const queryBtn = document.getElementById('query-btn');
    const exportBtn = document.getElementById('export-btn');
    const errorBox = document.getElementById('groups-error');
    const resultBox = document.getElementById('groups-result');
    const emptyBox = document.getElementById('groups-empty');
    const emptyDesc = document.getElementById('empty-desc');
    const cardsBox = document.getElementById('groups-cards');
    const syncedAt = document.getElementById('synced-at');
    let theatersFilled = false;
    let initialTheater = '';

    function escapeHtml(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function stateFromUrl() {
        const params = new URLSearchParams(location.search);
        return {
            keyword: (params.get('keyword') || '').trim() || DEFAULT_KEYWORD,
            theater: (params.get('theater') || '').trim(),
            q: (params.get('q') || '').trim(),
            only_robot: params.get('only_robot') === '1',
        };
    }

    function currentQuery() {
        return {
            keyword: (keywordInput.value || '').trim(),
            theater: theatersFilled ? theaterSelect.value : initialTheater,
            q: (storeQuery.value || '').trim(),
            only_robot: robotOnly.checked ? 1 : 0,
        };
    }

    function writeUrl(q) {
        const params = new URLSearchParams(location.search);
        if (q.keyword && q.keyword !== DEFAULT_KEYWORD) params.set('keyword', q.keyword);
        else params.delete('keyword');
        if (q.theater) params.set('theater', q.theater);
        else params.delete('theater');
        if (q.q) params.set('q', q.q);
        else params.delete('q');
        if (q.only_robot) params.set('only_robot', '1');
        else params.delete('only_robot');
        const qs = params.toString();
        history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
    }

    function requestParams(q) {
        const params = new URLSearchParams();
        params.set('keyword', q.keyword || '');
        params.set('theater', q.theater || '');
        params.set('q', q.q || '');
        params.set('only_robot', q.only_robot ? '1' : '0');
        return params.toString();
    }

    function cardHtml(title, value, hint) {
        return (
            '<div class="card"><h4>' + escapeHtml(title) + '</h4>' +
            '<div class="big-number" style="font-size:32px">' + value + '</div>' +
            '<div class="sub-stat">' + escapeHtml(hint) + '</div></div>'
        );
    }

    function fillTheaters(theaters, selected) {
        theaterSelect.innerHTML = '';
        const opt0 = document.createElement('option');
        opt0.value = '';
        opt0.textContent = '全部战区';
        theaterSelect.appendChild(opt0);
        (theaters || []).forEach((name) => {
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name;
            theaterSelect.appendChild(opt);
        });
        theaterSelect.value = theaters.indexOf(selected) >= 0 ? selected : '';
        theatersFilled = true;
    }

    function emptyRow(tbody, colspan, text) {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td colspan="' + colspan + '" style="text-align:center" class="cl-muted">' + escapeHtml(text) + '</td>';
        tbody.appendChild(tr);
    }

    function renderTheaterTable(data) {
        const tbody = document.querySelector('#theater-table tbody');
        const rows = data.by_theater || [];
        if (!rows.length) {
            emptyRow(tbody, 3, '暂无数据');
            return;
        }
        tbody.innerHTML = rows.map((row) =>
            '<tr class="theater-row" data-theater="' + escapeHtml(row.theater) + '" title="点击按该战区筛选">' +
            '<td data-label="战区" data-card-title>' + escapeHtml(row.theater) + '</td>' +
            '<td data-label="群数">' + row.group_count + '</td>' +
            '<td data-label="门店数">' + row.store_count + '</td></tr>'
        ).join('');
    }

    function renderStoreTable(data) {
        const tbody = document.querySelector('#store-table tbody');
        const rows = data.by_store || [];
        if (!rows.length) {
            emptyRow(tbody, 5, '暂无数据');
            return;
        }
        tbody.innerHTML = rows.map((row, idx) => {
            const theater = row.theater
                ? '<td data-label="战区" data-cards-full><button type="button" class="theater-chip" data-theater="' + escapeHtml(row.theater) + '" title="点击按该战区筛选">' + escapeHtml(row.theater) + '</button></td>'
                : '<td data-label="战区"><span class="cl-muted">—</span></td>';
            return '<tr><td data-label="排名">' + (idx + 1) + '</td>' +
                '<td data-label="门店" data-card-title>' + escapeHtml(row.store) + '</td>' +
                theater +
                '<td data-label="群数">' + row.group_count + '</td>' +
                '<td data-label="已拉机器人">' + (row.robot_count || 0) + '</td></tr>';
        }).join('');
    }

    function renderUnmatchedTable(data) {
        const tbody = document.querySelector('#unmatched-table tbody');
        const rows = data.unmatched || [];
        if (!rows.length) {
            emptyRow(tbody, 2, '匹配的群都已归店');
            return;
        }
        tbody.innerHTML = rows.map((row) =>
            '<tr><td data-label="群名" data-card-title>' + escapeHtml(row.group_name) + '</td>' +
            '<td data-label="是否已拉机器人">' + (row.in_robot ? '是' : '否') + '</td></tr>'
        ).join('');
    }

    function render(data) {
        if (!theatersFilled) fillTheaters(data.theaters, data.theater);
        syncedAt.textContent = data.synced_at ? '数据同步：' + data.synced_at : '按群名关键字统计';
        if (!data.total) {
            resultBox.hidden = true;
            emptyBox.hidden = false;
            emptyDesc.textContent = '关键字「' + data.keyword + '」' +
                (data.theater ? '（' + data.theater + '）' : '') +
                (data.q ? '、门店「' + data.q + '」' : '') +
                (data.only_robot ? '、仅已拉机器人群' : '') + ' 暂无匹配，调整关键字或筛选试试';
            return;
        }
        emptyBox.hidden = true;
        resultBox.hidden = false;
        cardsBox.innerHTML =
            cardHtml('匹配群总数', data.total, '群名含「' + data.keyword + '」' + (data.theater ? ' · ' + data.theater : '') + (data.q ? ' · 门店「' + data.q + '」' : '')) +
            cardHtml('已拉机器人群数', data.robot_total, '匹配群中已拉超时提醒机器人') +
            cardHtml('涉及门店数', data.matched_store_total, '匹配群按群主部门归店') +
            cardHtml('未归店群数', data.unmatched_total, '缺门店或战区信息');
        renderTheaterTable(data);
        renderStoreTable(data);
        renderUnmatchedTable(data);
    }

    async function query() {
        const q = currentQuery();
        errorBox.hidden = true;
        queryBtn.disabled = true;
        try {
            const res = await fetch('/api/groups/keyword?' + requestParams(q));
            const data = await res.json();
            if (!res.ok) {
                resultBox.hidden = true;
                emptyBox.hidden = true;
                errorBox.hidden = false;
                errorBox.textContent = data.error || '查询失败';
                return;
            }
            writeUrl(q);
            exportBtn.href = '/api/groups/keyword/export?' + requestParams(q);
            render(data);
        } catch (err) {
            errorBox.hidden = false;
            errorBox.textContent = '查询失败，请稍后重试';
        } finally {
            queryBtn.disabled = false;
        }
    }

    function filterByTheater(name) {
        if (!theatersFilled) return;
        theaterSelect.value = name;
        query();
    }

    queryBtn.addEventListener('click', query);
    keywordInput.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') query();
    });
    storeQuery.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') query();
    });
    theaterSelect.addEventListener('change', query);
    robotOnly.addEventListener('change', query);
    document.querySelector('#theater-table tbody').addEventListener('click', (ev) => {
        const tr = ev.target.closest('tr.theater-row');
        if (tr) filterByTheater(tr.dataset.theater);
    });
    document.querySelector('#store-table tbody').addEventListener('click', (ev) => {
        const btn = ev.target.closest('button.theater-chip');
        if (btn) filterByTheater(btn.dataset.theater);
    });

    const init = stateFromUrl();
    keywordInput.value = init.keyword;
    storeQuery.value = init.q;
    robotOnly.checked = init.only_robot;
    initialTheater = init.theater;
    query();
});
