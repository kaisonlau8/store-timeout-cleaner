document.addEventListener('DOMContentLoaded', () => {
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

            const allTableRows = Object.entries(allStores).map(([store, count], idx) =>
                '<tr><td>' + (idx + 1) + '</td><td>' + store + '</td><td>' + count + '</td></tr>'
            ).join('');

            const filterTableRows = Object.entries(filterStores).map(([store, count], idx) =>
                '<tr><td>' + (idx + 1) + '</td><td>' + store + '</td><td>' + count + '</td></tr>'
            ).join('');

            gridEl.innerHTML =
                '<div class="stat-block">' +
                    '<h3>全量建群</h3>' +
                    '<div class="big-number">' + data.all.group_count + '</div>' +
                    '<div class="sub-stat">涉及 <strong>' + data.all.store_count + '</strong> 家门店</div>' +
                    '<details class="store-details">' +
                        '<summary>查看各店群数量</summary>' +
                        '<div class="store-table-wrapper">' +
                            '<table class="store-table">' +
                                '<thead><tr><th>排名</th><th>门店名称</th><th>群数量</th></tr></thead>' +
                                '<tbody>' + allTableRows + '</tbody>' +
                            '</table>' +
                        '</div>' +
                    '</details>' +
                '</div>' +
                '<div class="stat-block">' +
                    '<h3>拉机器人群</h3>' +
                    '<div class="big-number">' + data.filter.group_count + '</div>' +
                    '<div class="sub-stat">涉及 <strong>' + data.filter.store_count + '</strong> 家门店</div>' +
                    '<details class="store-details">' +
                        '<summary>查看各店群数量</summary>' +
                        '<div class="store-table-wrapper">' +
                            '<table class="store-table">' +
                                '<thead><tr><th>排名</th><th>门店名称</th><th>群数量</th></tr></thead>' +
                                '<tbody>' + filterTableRows + '</tbody>' +
                            '</table>' +
                        '</div>' +
                    '</details>' +
                '</div>';
        });
    }
});
