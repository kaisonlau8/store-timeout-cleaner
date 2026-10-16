document.addEventListener('DOMContentLoaded', () => {
    const PALETTE = ['#1a1a2e', '#409eff', '#e6a23c', '#67c23a', '#f56c6c', '#909399'];
    let chart;
    let meta = { theaters: [], stores: [], rules: [] };

    const theaterSel = document.getElementById('theater-select');
    const storeInput = document.getElementById('store-input');
    const storeList = document.getElementById('store-list');
    const startDate = document.getElementById('start-date');
    const endDate = document.getElementById('end-date');
    const filterBtn = document.getElementById('filter-btn');
    const exportLink = document.getElementById('export-link');

    function localISODate() {
        const d = new Date();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return d.getFullYear() + '-' + m + '-' + day;
    }

    function fillTheaters() {
        theaterSel.innerHTML = '';
        const opt0 = document.createElement('option');
        opt0.value = '';
        opt0.textContent = '全国 / 全部战区';
        theaterSel.appendChild(opt0);
        (meta.theaters || []).forEach((name) => {
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name;
            theaterSel.appendChild(opt);
        });
    }

    function fillStores() {
        const theater = theaterSel.value;
        const stores = (meta.stores || []).filter((row) => !theater || row.theater === theater);
        storeList.innerHTML = '';
        stores.forEach((row) => {
            const opt = document.createElement('option');
            opt.value = row.name;
            storeList.appendChild(opt);
        });
        if (storeInput.value && !stores.some((row) => row.name === storeInput.value)) {
            storeInput.value = '';
        }
    }

    function selectedGrain() {
        const checked = document.querySelector('input[name=grain]:checked');
        return checked ? checked.value : 'month';
    }

    function currentQuery() {
        const store = (storeInput.value || '').trim();
        const theater = theaterSel.value;
        let scope = 'national';
        let id = '';
        if (store) {
            scope = 'store';
            id = store;
        } else if (theater) {
            scope = 'theater';
            id = theater;
        }
        return {
            scope,
            id,
            grain: selectedGrain(),
            start: startDate.value,
            end: endDate.value,
        };
    }

    function queryString(q) {
        const params = new URLSearchParams();
        Object.entries(q).forEach(([k, v]) => {
            if (v) params.set(k, v);
        });
        return params.toString();
    }

    function renderCards(data) {
        const totals = data.totals || {};
        document.getElementById('total-cards').innerHTML =
            cardHtml('计入超时的消息', totals.messages || 0, '去重后的客户消息') +
            cardHtml('规则触发次数', totals.triggers || 0, '各规则合计，一条消息可计多次') +
            cardHtml('触发群数', totals.rooms || 0, '有超时计入的客户群') +
            cardHtml('涉及门店', totals.stores || 0, '当前筛选范围内');

        const ruleCards = document.getElementById('rule-cards');
        const rules = totals.rules || [];
        if (!rules.length) {
            ruleCards.innerHTML = '';
            return;
        }
        ruleCards.innerHTML = rules.map((rule) =>
            cardHtml(
                rule.rule_name || rule.rule_id,
                rule.triggers || 0,
                '消息 ' + (rule.messages || 0) + ' · 群 ' + (rule.rooms || 0)
            )
        ).join('');
    }

    function cardHtml(title, value, hint) {
        return (
            '<div class="card">' +
                '<h4>' + escapeHtml(title) + '</h4>' +
                '<div class="big-number" style="font-size:32px">' + value + '</div>' +
                '<div class="sub-stat">' + escapeHtml(hint) + '</div>' +
            '</div>'
        );
    }

    function escapeHtml(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function renderChart(data) {
        if (typeof Chart === 'undefined') {
            document.querySelector('.chart-hint').textContent = '图表库未加载，门店明细仍可查看';
            return;
        }
        const ctx = document.getElementById('timeout-chart');
        const datasets = (data.series || []).map((series, idx) => {
            const color = PALETTE[idx % PALETTE.length];
            const isMsg = series.key === 'messages';
            return {
                label: series.name,
                data: series.data,
                borderColor: color,
                backgroundColor: color,
                borderDash: isMsg ? [] : [6, 4],
                borderWidth: isMsg ? 3 : 2,
                tension: 0.25,
                pointRadius: data.labels.length > 40 ? 0 : 3,
                fill: false,
            };
        });
        if (chart) chart.destroy();
        chart = new Chart(ctx, {
            type: 'line',
            data: { labels: data.labels || [], datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { position: 'bottom' },
                },
                scales: {
                    y: { beginAtZero: true, ticks: { precision: 0 } },
                },
            },
        });
    }

    function renderTable(data) {
        const rules = (data.totals && data.totals.rules) || [];
        const thead = document.querySelector('#store-table thead');
        const tbody = document.querySelector('#store-table tbody');
        let head = '<tr><th>排名</th><th>门店</th><th>战区</th><th>计入消息</th><th>触发群数</th><th>规则次数</th>';
        rules.forEach((rule) => {
            head += '<th>' + escapeHtml(rule.rule_name || rule.rule_id) + '</th>';
        });
        head += '</tr>';
        thead.innerHTML = head;

        const rows = data.stores || [];
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="' + (6 + rules.length) + '" style="text-align:center;color:#909399">当前筛选暂无数据</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map((row, idx) => {
            let html = '<tr><td>' + (idx + 1) + '</td><td>' + escapeHtml(row.store || '') +
                '</td><td>' + escapeHtml(row.theater || '') +
                '</td><td>' + (row.messages || 0) +
                '</td><td>' + (row.rooms || 0) +
                '</td><td>' + (row.triggers || 0) + '</td>';
            rules.forEach((rule) => {
                const cell = (row.by_rule || {})[rule.rule_id] || {};
                html += '<td>' + (cell.triggers || 0) + '</td>';
            });
            return html + '</tr>';
        }).join('');
    }

    let loadDepth = 0;

    function pushLoad(tip) {
        loadDepth += 1;
        window.AntdFeedback.setButtonLoading(filterBtn, true);
        window.AntdFeedback.setSpin(document.getElementById('timeout-results'), true, tip || '正在查询');
    }

    function popLoad() {
        loadDepth = Math.max(0, loadDepth - 1);
        if (loadDepth > 0) return;
        window.AntdFeedback.setButtonLoading(filterBtn, false);
        window.AntdFeedback.setSpin(document.getElementById('timeout-results'), false);
    }

    async function queryChart(options) {
        const nested = options && options.nested;
        const q = currentQuery();
        exportLink.href = '/api/timeout/export?' + queryString(q);
        if (!nested) pushLoad('正在查询');
        try {
            const res = await fetch('/api/timeout/chart?' + queryString(q));
            let data = {};
            try { data = await res.json(); } catch (e) { data = {}; }
            if (!res.ok) throw new Error((data && data.error) || '查询失败');
            renderCards(data);
            renderChart(data);
            renderTable(data);
        } catch (err) {
            if (nested) throw err;
            window.AntdFeedback.message('error', (err && err.message) || '查询失败');
        } finally {
            if (!nested) popLoad();
        }
    }

    document.querySelectorAll('.grain-pills input').forEach((input) => {
        input.addEventListener('change', () => {
            document.querySelectorAll('.grain-pills .toggle-btn').forEach((el) => el.classList.remove('active'));
            input.closest('.toggle-btn').classList.add('active');
        });
    });

    theaterSel.addEventListener('change', fillStores);
    filterBtn.addEventListener('click', queryChart);
    storeInput.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') queryChart();
    });

    function renderImports(imports) {
        const tbody = document.querySelector('#import-table tbody');
        if (!tbody) return;
        const rows = imports || [];
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#909399">尚未导入。把管家导出放到 input_file/ 或点右上角上传</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map((row) =>
            '<tr><td>' + escapeHtml((row.range_start || '') + ' ~ ' + (row.range_end || '')) +
            '</td><td>' + escapeHtml(row.filename || '') +
            '</td><td>' + (row.rows_in_file || 0) +
            '</td><td>' + (row.events || 0) +
            '</td><td>' + (row.unmatched || 0) +
            '</td><td>' + escapeHtml(row.imported_at || '') + '</td></tr>'
        ).join('');
    }

    function applyMeta(payload) {
        meta = payload || meta;
        fillTheaters();
        fillStores();
        if (meta.min_date) startDate.value = meta.min_date;
        if (meta.max_date) endDate.value = meta.max_date;
        else endDate.value = localISODate();
        const el = document.getElementById('updated-at');
        if (meta.updated_at) {
            el.textContent = '最近导入：' + meta.updated_at + ' · 管家回复超时记录 Excel';
        } else {
            el.textContent = '导入管家「回复超时记录」Excel，按文件名时间范围登记';
        }
        renderImports(meta.imports);
    }

    function setImportProgress(fileName, percent, status) {
        const box = document.getElementById('import-progress');
        const bg = document.getElementById('import-progress-bg');
        const text = document.getElementById('import-progress-text');
        const name = document.getElementById('import-progress-name');
        const bar = document.getElementById('import-progress-bar');
        if (!box || !bg || !text || !name || !bar) return;
        box.hidden = false;
        name.textContent = fileName || '';
        const shown = Math.max(0, Math.min(100, percent || 0));
        bg.style.width = shown + '%';
        text.textContent = shown >= 100 && status !== 'exception' ? '处理中' : (shown + '%');
        bar.classList.remove('ant-progress-status-active', 'ant-progress-status-success', 'ant-progress-status-exception');
        if (status === 'exception') bar.classList.add('ant-progress-status-exception');
        else if (status === 'success' || shown >= 100) bar.classList.add('ant-progress-status-success');
        else bar.classList.add('ant-progress-status-active');
    }

    function hideImportProgress() {
        const box = document.getElementById('import-progress');
        if (box) box.hidden = true;
    }

    function importSummary(data) {
        if (!data || !data.filename) return '已写入超时记录';
        return (data.filename || '') +
            ' · ' + (data.range_start || '') + ' ~ ' + (data.range_end || '') +
            ' · 文件 ' + (data.rows_in_file || 0) + ' 行 · 计入 ' + (data.events || 0) +
            ' · 未归店 ' + (data.unmatched || 0);
    }

    async function reloadAll() {
        const importPanel = document.getElementById('import-panel');
        pushLoad('正在加载');
        window.AntdFeedback.setSpin(importPanel, true, '正在加载');
        try {
            const res = await fetch('/api/timeout/meta?scan=1');
            let payload = {};
            try { payload = await res.json(); } catch (e) { payload = {}; }
            if (!res.ok) throw new Error((payload && payload.error) || '加载失败');
            applyMeta(payload);
            await queryChart({ nested: true });
        } catch (err) {
            window.AntdFeedback.message('error', '页面数据加载失败');
            if (!endDate.value) endDate.value = localISODate();
        } finally {
            window.AntdFeedback.setSpin(importPanel, false);
            popLoad();
        }
    }

    const fileInput = document.getElementById('excel-file');
    const importError = document.getElementById('import-error');
    const uploadBtn = document.getElementById('import-upload');
    if (fileInput) {
        fileInput.addEventListener('change', async () => {
            const file = fileInput.files && fileInput.files[0];
            if (!file) return;
            importError.hidden = true;
            window.AntdFeedback.setButtonLoading(uploadBtn, true);
            setImportProgress(file.name, 0, 'active');
            try {
                const data = await window.AntdFeedback.upload('/api/timeout/import', file, {
                    field: 'file',
                    onProgress: (pct) => setImportProgress(file.name, pct, pct >= 100 ? 'success' : 'active'),
                });
                setImportProgress(file.name, 100, 'success');
                window.AntdFeedback.notification('success', '导入完成', importSummary(data));
                hideImportProgress();
                await reloadAll();
            } catch (err) {
                const msg = (err && err.message) || '导入失败';
                setImportProgress(file.name, 100, 'exception');
                importError.hidden = false;
                importError.textContent = msg;
                window.AntdFeedback.message('error', msg);
            } finally {
                window.AntdFeedback.setButtonLoading(uploadBtn, false);
                fileInput.value = '';
            }
        });
    }

    reloadAll();
});
