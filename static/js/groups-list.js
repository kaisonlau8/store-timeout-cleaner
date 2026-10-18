/* 群管理：群列表页签的筛选、分页与页签切换。 */
(function () {
    'use strict';

    var PAGE_SIZE = 50;
    var tabButtons = Array.prototype.slice.call(document.querySelectorAll('#groups-tabs .tab-btn'));
    var tabPanels = {
        list: document.getElementById('tab-list'),
        keyword: document.getElementById('tab-keyword')
    };
    var activeTab = 'list';

    function esc(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
        });
    }

    function currentParams() {
        return new URLSearchParams(location.search);
    }

    function setTab(name, write) {
        activeTab = name === 'keyword' ? 'keyword' : 'list';
        tabButtons.forEach(function (button) {
            var on = button.dataset.tab === activeTab;
            button.classList.toggle('active', on);
            button.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        Object.keys(tabPanels).forEach(function (key) {
            var panel = tabPanels[key];
            if (panel) panel.classList.toggle('active', key === activeTab);
        });
        if (write) {
            var params = currentParams();
            params.set('tab', activeTab);
            var query = params.toString();
            history.replaceState(null, '', location.pathname + (query ? '?' + query : ''));
        }
    }

    function readListState() {
        var params = currentParams();
        var robot = params.get('robot') || 'all';
        if (robot !== '1' && robot !== '0') robot = 'all';
        var page = parseInt(params.get('page') || '1', 10);
        if (!isFinite(page) || page < 1) page = 1;
        return {
            q: (params.get('q') || '').trim(),
            theater: (params.get('theater') || '').trim(),
            store: (params.get('store') || '').trim(),
            robot: robot,
            page: page
        };
    }

    function writeListState(state) {
        if (activeTab !== 'list') return;
        var params = currentParams();
        params.set('tab', 'list');
        if (state.q) params.set('q', state.q); else params.delete('q');
        if (state.theater) params.set('theater', state.theater); else params.delete('theater');
        if (state.store) params.set('store', state.store); else params.delete('store');
        if (state.robot !== 'all') params.set('robot', state.robot); else params.delete('robot');
        if (state.page > 1) params.set('page', String(state.page)); else params.delete('page');
        var query = params.toString();
        history.replaceState(null, '', location.pathname + (query ? '?' + query : ''));
    }

    function setSpin(target, on, tip) {
        if (window.AntdFeedback && typeof window.AntdFeedback.setSpin === 'function') {
            window.AntdFeedback.setSpin(target, on, tip);
        }
    }

    function dateText(value) {
        var text = String(value == null ? '' : value).trim();
        if (!text) return '<span class="cl-muted">—</span>';
        var match = /^(\d{4}-\d{2}-\d{2})/.exec(text);
        return esc(match ? match[1] : text);
    }

    function pageText(page, totalPages, total) {
        if (window.matchMedia && window.matchMedia('(max-width: 860px)').matches) {
            return page + ' / ' + totalPages + ' 页 · ' + total + ' 个群';
        }
        return '第 ' + page + ' / ' + totalPages + ' 页 · 共 ' + total + ' 个群';
    }

    function renderRows(rows) {
        var tbody = document.querySelector('#groups-list-table tbody');
        if (!tbody) return;
        if (!rows.length) {
            tbody.innerHTML = '';
            return;
        }
        tbody.innerHTML = rows.map(function (row) {
            var roomId = row.room_id || '';
            var groupName = row.group_name || roomId || '（未命名群）';
            var groupCell = '<a data-ds-link="off" href="/group/' + encodeURIComponent(roomId) + '">' + esc(groupName) + '</a>';
            var storeCell = row.store
                ? '<a data-ds-link="off" href="/store/' + encodeURIComponent(row.store) + '">' + esc(row.store) + '</a>'
                : '<span class="cl-muted">未归店</span>';
            var theaterCell = row.theater
                ? esc(row.theater)
                : '<span class="cl-muted">未归战区</span>';
            var robotCell = row.in_robot
                ? '<span class="ant-tag ant-tag-success">已拉机器人</span>'
                : '<span class="ant-tag">未拉</span>';
            var ownerCell = row.owner_name
                ? esc(row.owner_name)
                : '<span class="cl-muted">未同步</span>';
            var memberCell = (row.member_count === null || row.member_count === undefined || row.member_count === '')
                ? '<span class="cl-muted">未同步</span>'
                : esc(row.member_count);
            return '<tr>' +
                '<td data-label="群名" data-card-title>' + groupCell + '</td>' +
                '<td data-label="门店">' + storeCell + '</td>' +
                '<td data-label="战区">' + theaterCell + '</td>' +
                '<td data-label="机器人">' + robotCell + '</td>' +
                '<td data-label="群主">' + ownerCell + '</td>' +
                '<td data-label="群人数">' + memberCell + '</td>' +
                '<td data-label="创建时间">' + dateText(row.create_time) + '</td></tr>';
        }).join('');
    }

    function init() {
        var qInput = document.getElementById('list-q');
        var theaterSelect = document.getElementById('list-theater-select');
        var storeInput = document.getElementById('list-store-q');
        var robotSelect = document.getElementById('list-robot-select');
        var queryButton = document.getElementById('groups-list-query');
        var errorBox = document.getElementById('groups-list-error');
        var tableWrap = document.getElementById('groups-list-table-wrap');
        var emptyBox = document.getElementById('groups-list-empty');
        var pageInfo = document.getElementById('groups-list-page-info');
        var prevButton = document.getElementById('groups-list-prev');
        var nextButton = document.getElementById('groups-list-next');
        var state = readListState();
        var theatersFilled = false;
        var loaded = false;

        if (!qInput || !theaterSelect || !storeInput || !robotSelect || !queryButton) return;

        qInput.value = state.q;
        storeInput.value = state.store;
        robotSelect.value = state.robot;

        function fillTheaters(theaters) {
            theaterSelect.innerHTML = '';
            var all = document.createElement('option');
            all.value = '';
            all.textContent = '全部战区';
            theaterSelect.appendChild(all);
            (theaters || []).forEach(function (name) {
                var option = document.createElement('option');
                option.value = name;
                option.textContent = name;
                theaterSelect.appendChild(option);
            });
            theaterSelect.value = (theaters || []).indexOf(state.theater) >= 0 ? state.theater : '';
            state.theater = theaterSelect.value;
            theatersFilled = true;
        }

        function resultParams() {
            var params = new URLSearchParams();
            params.set('q', state.q);
            params.set('theater', state.theater);
            params.set('store', state.store);
            params.set('robot', state.robot);
            params.set('page', String(state.page));
            params.set('size', String(PAGE_SIZE));
            return params.toString();
        }

        function render(data) {
            var rows = data.rows || [];
            var size = data.size || PAGE_SIZE;
            var totalPages = Math.max(1, Math.ceil((data.total || 0) / size));
            renderRows(rows);
            if (tableWrap) tableWrap.hidden = rows.length === 0;
            if (emptyBox) emptyBox.hidden = rows.length !== 0;
            if (pageInfo) {
                pageInfo.textContent = pageText(data.page || 1, totalPages, data.total || 0);
            }
            if (prevButton) prevButton.disabled = (data.page || 1) <= 1;
            if (nextButton) nextButton.disabled = (data.page || 1) >= totalPages || !data.total;
        }

        async function query(page) {
            if (page != null) state.page = page;
            if (pageInfo) pageInfo.textContent = '加载中…';
            if (errorBox) errorBox.hidden = true;
            queryButton.disabled = true;
            setSpin(tableWrap, true, '加载群列表');
            try {
                var response = await fetch('/api/groups/list?' + resultParams(), { credentials: 'same-origin' });
                var data = await response.json().catch(function () { return {}; });
                if (!response.ok) throw new Error(data.error || data.description || '群列表加载失败');
                state.page = data.page || state.page;
                if (!theatersFilled) fillTheaters(data.theaters || []);
                render(data);
                loaded = true;
                writeListState(state);
            } catch (error) {
                if (tableWrap) tableWrap.hidden = true;
                if (emptyBox) emptyBox.hidden = true;
                if (errorBox) {
                    errorBox.hidden = false;
                    errorBox.textContent = error.message || '群列表加载失败，请稍后重试';
                }
                if (pageInfo) pageInfo.textContent = '第 — / — 页 · 共 0 个群';
            } finally {
                setSpin(tableWrap, false);
                queryButton.disabled = false;
            }
        }

        function queryFromInputs() {
            state.q = (qInput.value || '').trim();
            state.theater = theaterSelect.value || '';
            state.store = (storeInput.value || '').trim();
            state.robot = robotSelect.value || 'all';
            state.page = 1;
            query(1);
        }

        queryButton.addEventListener('click', queryFromInputs);
        [qInput, storeInput].forEach(function (input) {
            input.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    queryFromInputs();
                }
            });
        });
        theaterSelect.addEventListener('change', queryFromInputs);
        robotSelect.addEventListener('change', queryFromInputs);
        if (prevButton) {
            prevButton.addEventListener('click', function () {
                if (state.page > 1) query(state.page - 1);
            });
        }
        if (nextButton) {
            nextButton.addEventListener('click', function () {
                query(state.page + 1);
            });
        }

        tabButtons.forEach(function (button) {
            button.addEventListener('click', function () {
                setTab(button.dataset.tab, true);
                if (activeTab === 'list' && !loaded) query(state.page);
            });
        });

        var initialTab = currentParams().get('tab') === 'keyword' ? 'keyword' : 'list';
        setTab(initialTab, false);
        query(state.page);
        window.GroupsList = { query: query, state: state };
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
