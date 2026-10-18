/* 门店页群名单：超过 50 行时先显示前 50 行，按钮每次多显示 50 行（纯前端）。 */
(function () {
    'use strict';

    var PAGE_SIZE = 50;

    function init() {
        var boxes = document.querySelectorAll('[data-group-pager]');
        Array.prototype.forEach.call(boxes, function (box) {
            var tbody = box.querySelector('tbody');
            if (!tbody) return;
            var rows = [];
            Array.prototype.forEach.call(tbody.rows, function (row) {
                if (row.cells.length === 1 && row.cells[0].colSpan > 1) return;
                if (row.classList.contains('tot')) return;
                rows.push(row);
            });
            var size = parseInt(box.getAttribute('data-page-size'), 10);
            if (!size || size < 1) size = PAGE_SIZE;
            var shown = size;
            var button = box.querySelector('[data-group-more]');
            if (!button && rows.length > size) {
                var wrap = document.createElement('div');
                wrap.className = 'group-more';
                button = document.createElement('button');
                button.type = 'button';
                button.className = 'filter-btn';
                button.setAttribute('data-group-more', '');
                wrap.appendChild(button);
                box.appendChild(wrap);
            }
            function render() {
                rows.forEach(function (row, index) {
                    if (index < shown) row.removeAttribute('hidden');
                    else row.setAttribute('hidden', 'hidden');
                });
                if (!button) return;
                var remaining = Math.max(0, rows.length - shown);
                button.hidden = remaining === 0;
                button.textContent = '显示更多（剩 ' + remaining + '）';
            }
            if (button && button.getAttribute('data-bound') !== '1') {
                button.setAttribute('data-bound', '1');
                button.addEventListener('click', function () {
                    shown = Math.min(rows.length, shown + size);
                    render();
                });
            }
            render();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
    window.StcStorePage = { init: init };
})();
