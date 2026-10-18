function coverageStoreHref(name) {
    const params = new URLSearchParams();
    const current = new URLSearchParams(window.location.search);
    const theater = (current.get('theater') || '').trim();
    const q = (current.get('q') || '').trim();
    if (theater) params.set('theater', theater);
    if (q) params.set('back_q', q);
    return '/store/' + encodeURIComponent(name) + (params.size ? '?' + params.toString() : '');
}

function theaterPageHref(name) {
    const params = new URLSearchParams();
    const current = new URLSearchParams(window.location.search);
    const start = (current.get('start') || '').trim();
    const end = (current.get('end') || '').trim();
    if (start) params.set('start', start);
    if (end) params.set('end', end);
    return '/theater/' + encodeURIComponent(name) + (params.size ? '?' + params.toString() : '');
}

document.addEventListener('DOMContentLoaded', () => {
    function escapeHtml(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function listFilter() {
        return window.LIST_FILTER || { theater: '', q: '' };
    }

    function storeMeta(name) {
        return (window.STORE_META || {})[name] || {};
    }

    function visibleStore(name) {
        const meta = storeMeta(name);
        const filter = listFilter();
        const theater = filter.theater || '';
        const q = String(filter.q || '').trim().toLowerCase();
        if (theater && (meta.theater || '') !== theater) return false;
        if (!q) return true;
        const code = String(meta.code || '').toLowerCase();
        const label = String(name || '').toLowerCase();
        return label.indexOf(q) >= 0 || (code && code.indexOf(q) >= 0);
    }

    function mergedRows(allStores, filterStores) {
        const names = new Set([
            ...Object.keys(allStores || {}),
            ...Object.keys(filterStores || {}),
        ]);
        const rows = [];
        names.forEach((name) => {
            if (!visibleStore(name)) return;
            rows.push({
                name: name,
                theater: storeMeta(name).theater || '',
                group_count: Number((allStores || {})[name] || 0),
                robot_group_count: Number((filterStores || {})[name] || 0),
            });
        });
        rows.sort((a, b) =>
            b.group_count - a.group_count ||
            b.robot_group_count - a.robot_group_count ||
            a.name.localeCompare(b.name, 'zh')
        );
        return rows;
    }

    function coverageTable(rows) {
        const body = rows.length
            ? rows.map((row, idx) =>
                '<tr><td>' + (idx + 1) + '</td>' +
                '<td data-card-title><a class="store-link" href="' + coverageStoreHref(row.name) + '">' + escapeHtml(row.name) + '</a></td>' +
                '<td>' + (row.theater
                    ? '<a class="store-link" href="' + theaterPageHref(row.theater) + '">' + escapeHtml(row.theater) + '<svg class="ai" aria-hidden="true"><use href="#i-right"/></svg></a>'
                    : '—') + '</td>' +
                '<td>' + row.group_count + '</td>' +
                '<td>' + row.robot_group_count + '</td></tr>'
            ).join('')
            : '<tr><td colspan="5" style="text-align:center" class="cl-muted">当前筛选没有门店</td></tr>';
        return (
            '<div class="store-table-wrapper">' +
                '<table class="store-table">' +
                    '<thead><tr><th>排名</th><th data-card-title>门店名称</th><th>战区</th><th>全量群数</th><th>已拉机器人群数</th></tr></thead>' +
                    '<tbody>' + body + '</tbody>' +
                '</table>' +
            '</div>'
        );
    }

    // Tab 切换
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
        });
    });

    // 自定义下拉框
    document.querySelectorAll('.custom-select').forEach(sel => {
        const trigger = sel.querySelector('.select-trigger');
        const options = sel.querySelectorAll('.select-option');

        trigger.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = sel.classList.contains('open');
            document.querySelectorAll('.custom-select').forEach(s => s.classList.remove('open'));
            if (!isOpen) sel.classList.add('open');
        });

        options.forEach(opt => {
            opt.addEventListener('click', (e) => {
                e.stopPropagation();
                options.forEach(o => o.classList.remove('selected'));
                opt.classList.add('selected');
                trigger.textContent = opt.textContent;
                trigger.dataset.value = opt.dataset.value;
                sel.classList.remove('open');
            });
        });
    });

    document.addEventListener('click', () => {
        document.querySelectorAll('.custom-select').forEach(s => s.classList.remove('open'));
    });

    // 月份筛选按钮
    const filterBtn = document.getElementById('filter-month-btn');
    if (filterBtn) {
        filterBtn.addEventListener('click', () => {
            const year = document.querySelector('#year-select-wrap .select-trigger').dataset.value;
            const month = document.querySelector('#month-select-wrap .select-trigger').dataset.value;
            const key = year + '-' + month;
            const data = window.MONTHLY_DATA[key];

            const titleEl = document.getElementById('compare-title');
            const gridEl = document.getElementById('compare-grid');

            if (!data) {
                titleEl.textContent = key + ' 暂无数据';
                gridEl.innerHTML = '<p style="color:#999;text-align:center;padding:40px;">该月份暂无数据</p>';
                return;
            }

            titleEl.textContent = key + ' 月度对比';

            const allStores = data.all.store_stats || {};
            const filterStores = data.filter.store_stats || {};
            const rows = mergedRows(allStores, filterStores);
            const allRows = rows.filter((row) => row.group_count > 0);
            const robotRows = rows.filter((row) => row.robot_group_count > 0);
            const allGroups = allRows.reduce((sum, row) => sum + row.group_count, 0);
            const robotGroups = robotRows.reduce((sum, row) => sum + row.robot_group_count, 0);

            gridEl.innerHTML =
                '<div class="stat-block">' +
                    '<h3>全量建群</h3>' +
                    '<div class="big-number">' + allGroups + '</div>' +
                    '<div class="sub-stat">涉及 <strong>' + allRows.length + '</strong> 家门店</div>' +
                    '<details class="store-details">' +
                        '<summary>查看各店群数量</summary>' +
                        coverageTable(allRows) +
                    '</details>' +
                '</div>' +
                '<div class="stat-block">' +
                    '<h3>拉机器人群</h3>' +
                    '<div class="big-number">' + robotGroups + '</div>' +
                    '<div class="sub-stat">涉及 <strong>' + robotRows.length + '</strong> 家门店</div>' +
                    '<details class="store-details">' +
                        '<summary>查看各店群数量</summary>' +
                        coverageTable(robotRows) +
                    '</details>' +
                '</div>';
        });
    }
});
