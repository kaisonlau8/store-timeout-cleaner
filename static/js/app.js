document.addEventListener('DOMContentLoaded', () => {
    const allUpload = document.getElementById('all-upload');
    const filterUpload = document.getElementById('filter-upload');
    const allFileInput = document.getElementById('all-file');
    const filterFileInput = document.getElementById('filter-file');
    const allFilename = document.getElementById('all-filename');
    const filterFilename = document.getElementById('filter-filename');
    const analyzeBtn = document.getElementById('analyze-btn');
    const errorMsg = document.getElementById('error-msg');
    const results = document.getElementById('results');

    let allFile = null;
    let filterFile = null;

    function setupUpload(box, input, filenameEl, type) {
        box.addEventListener('click', () => input.click());

        box.addEventListener('dragover', (e) => {
            e.preventDefault();
            box.classList.add('dragover');
        });

        box.addEventListener('dragleave', () => {
            box.classList.remove('dragover');
        });

        box.addEventListener('drop', (e) => {
            e.preventDefault();
            box.classList.remove('dragover');
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                handleFile(files[0], type);
            }
        });

        input.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handleFile(e.target.files[0], type);
            }
        });
    }

    function handleFile(file, type) {
        if (type === 'all') {
            allFile = file;
            allFilename.textContent = file.name;
            document.getElementById('all-upload').classList.add('has-file');
        } else {
            filterFile = file;
            filterFilename.textContent = file.name;
            document.getElementById('filter-upload').classList.add('has-file');
        }
        checkReady();
    }

    function checkReady() {
        if (allFile && filterFile) {
            analyzeBtn.disabled = false;
        }
    }

    setupUpload(allUpload, allFileInput, allFilename, 'all');
    setupUpload(filterUpload, filterFileInput, filterFilename, 'filter');

    analyzeBtn.addEventListener('click', async () => {
        if (!allFile || !filterFile) return;

        analyzeBtn.disabled = true;
        analyzeBtn.querySelector('.btn-text').hidden = true;
        analyzeBtn.querySelector('.btn-loading').hidden = false;
        errorMsg.hidden = true;
        results.hidden = true;

        const formData = new FormData();
        formData.append('all_file', allFile);
        formData.append('filter_file', filterFile);

        try {
            const res = await fetch('/api/upload', {
                method: 'POST',
                body: formData,
            });
            const data = await res.json();

            if (!data.success) {
                throw new Error(data.error || '分析失败');
            }

            renderResults(data);
            results.hidden = false;
        } catch (err) {
            errorMsg.textContent = '错误: ' + err.message;
            errorMsg.hidden = false;
        } finally {
            analyzeBtn.disabled = false;
            analyzeBtn.querySelector('.btn-text').hidden = false;
            analyzeBtn.querySelector('.btn-loading').hidden = true;
        }
    });

    function renderResults(data) {
        const s = data.summary;
        const stats = data.stats;

        document.getElementById('all-total').textContent = s.all_total;
        document.getElementById('all-valid').textContent = s.all_valid;
        document.getElementById('all-invalid').textContent = s.all_invalid;
        document.getElementById('filter-total').textContent = s.filter_total;
        document.getElementById('filter-valid').textContent = s.filter_valid;
        document.getElementById('filter-invalid').textContent = s.filter_invalid;

        // 全国统计
        const nat = stats.national;
        document.getElementById('nat-all-groups').textContent = nat.all.group_count;
        document.getElementById('nat-all-stores').textContent = nat.all.store_count;
        renderStoreTable('nat-all-table', nat.all.store_stats);

        document.getElementById('nat-filter-groups').textContent = nat.filter.group_count;
        document.getElementById('nat-filter-stores').textContent = nat.filter.store_count;
        renderStoreTable('nat-filter-table', nat.filter.store_stats);

        // 5月1日后
        const after = stats.after_may1;
        document.getElementById('after-all-groups').textContent = after.all.group_count;
        document.getElementById('after-all-stores').textContent = after.all.store_count;
        renderStoreTable('after-all-table', after.all.store_stats);

        document.getElementById('after-filter-groups').textContent = after.filter.group_count;
        document.getElementById('after-filter-stores').textContent = after.filter.store_count;
        renderStoreTable('after-filter-table', after.filter.store_stats);

        // 按月统计
        renderMonthly(stats.monthly.all, 'monthly-all-list');
        renderMonthly(stats.monthly.filter, 'monthly-filter-list');
    }

    function renderStoreTable(tableId, storeStats) {
        const table = document.getElementById(tableId);
        const entries = Object.entries(storeStats);
        let html = '<thead><tr><th>排名</th><th>门店名称</th><th>群数量</th></tr></thead><tbody>';
        entries.forEach(([store, count], idx) => {
            html += `<tr><td>${idx + 1}</td><td>${store}</td><td>${count}</td></tr>`;
        });
        html += '</tbody>';
        table.innerHTML = html;
    }

    function renderMonthly(monthlyData, containerId) {
        const container = document.getElementById(containerId);
        if (!monthlyData.length) {
            container.innerHTML = '<p style="color:#999;text-align:center;padding:20px;">暂无数据</p>';
            return;
        }
        let html = '';
        monthlyData.forEach(m => {
            const storeEntries = Object.entries(m.store_stats);
            let tableHtml = '<table class="month-store-table"><thead><tr><th>排名</th><th>门店名称</th><th>群数量</th></tr></thead><tbody>';
            storeEntries.forEach(([store, count], idx) => {
                tableHtml += `<tr><td>${idx + 1}</td><td>${store}</td><td>${count}</td></tr>`;
            });
            tableHtml += '</tbody></table>';

            html += `
                <div class="month-card">
                    <h4>${m.month}</h4>
                    <div class="month-stats">
                        <div class="month-stat">
                            <span class="num">${m.group_count}</span>
                            <span class="label">群数量</span>
                        </div>
                        <div class="month-stat">
                            <span class="num">${m.store_count}</span>
                            <span class="label">门店数量</span>
                        </div>
                    </div>
                    <details>
                        <summary style="cursor:pointer;color:#409eff;font-size:14px;font-weight:500;">查看各店群数量</summary>
                        <div style="margin-top:12px;max-height:300px;overflow-y:auto;">
                            ${tableHtml}
                        </div>
                    </details>
                </div>
            `;
        });
        container.innerHTML = html;
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

    // 按月统计切换
    document.querySelectorAll('.toggle-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const type = btn.dataset.type;
            document.getElementById('monthly-all').hidden = type !== 'all';
            document.getElementById('monthly-filter').hidden = type !== 'filter';
        });
    });
});
