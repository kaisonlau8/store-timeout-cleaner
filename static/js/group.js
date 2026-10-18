/* 群详情页：行内聊天上下文、群成员懒加载、?event= 定位。 */
(function () {
    'use strict';

    function esc(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
        });
    }

    function qs(params) {
        return Object.keys(params).filter(function (key) {
            return params[key] !== '' && params[key] != null;
        }).map(function (key) {
            return encodeURIComponent(key) + '=' + encodeURIComponent(params[key]);
        }).join('&');
    }

    function roleLabel(role) {
        return { customer: '客户', staff: '员工', robot: '机器人' }[role] || '未知';
    }

    function sourceLabel(source) {
        return source === 'scrm' ? 'SCRM 实时' : '本地存档';
    }

    function contextHtml(data) {
        var messages = data.messages || [];
        var html = '<div class="group-context-box"><p class="group-context-source">来源：' +
            esc(sourceLabel(data.source)) + '</p>';
        if (!messages.length) {
            return html + '<div class="sd-empty">该时间段暂无聊天记录</div></div>';
        }
        html += messages.map(function (msg) {
            var who = msg.sender || msg.role_label || roleLabel(msg.role);
            var when = String(msg.time || '');
            return '<div class="sd-msg role-' + esc(msg.role) + (msg.trigger ? ' trigger' : '') + '">' +
                '<span class="who">' + esc(who) + '</span>' +
                '<span class="when">' + esc(when.length > 10 ? when.slice(11) : when) + '</span>' +
                '<div class="txt">' + esc(msg.text) + '</div></div>';
        }).join('');
        return html + '</div>';
    }

    function membersHtml(data) {
        var list = data.members || [];
        var html = '<div class="group-context-box">';
        if (!list.length) {
            return html + '<div class="sd-empty">SCRM 未返回该群成员</div></div>';
        }
        var counts = { staff: 0, customer: 0, robot: 0 };
        list.forEach(function (member) {
            if (counts[member.role] != null) counts[member.role] += 1;
        });
        html += '<p class="sd-sub">共 ' + list.length + ' 人：员工 ' + counts.staff +
            ' · 客户 ' + counts.customer + ' · 机器人 ' + counts.robot +
            '（实时取自 SCRM，缓存 10 分钟）</p>';
        html += '<table class="store-table"><thead><tr><th>姓名</th><th>角色</th><th>入群时间</th></tr></thead><tbody>';
        html += list.map(function (member) {
            return '<tr><td>' + esc(member.name || '（未知）') + '</td>' +
                '<td><span class="ant-tag">' + esc(member.role_label || roleLabel(member.role)) + '</span></td>' +
                '<td>' + esc(member.join_time || '未知') + '</td></tr>';
        }).join('');
        return html + '</tbody></table></div>';
    }

    function init() {
        var holder = document.querySelector('[data-group-room]');
        if (!holder) return;
        var roomId = holder.getAttribute('data-group-room') || '';

        function closeContext(row, button) {
            var next = row.nextElementSibling;
            if (next && next.classList.contains('group-context-row')) {
                next.parentNode.removeChild(next);
            }
            button.setAttribute('aria-expanded', 'false');
            button.textContent = '聊天上下文';
        }

        function toggleContext(button) {
            var row = button.closest('tr[data-msg]');
            if (!row) return;
            if (button.getAttribute('aria-expanded') === 'true') {
                closeContext(row, button);
                return;
            }
            var tr = document.createElement('tr');
            tr.className = 'group-context-row';
            var td = document.createElement('td');
            td.colSpan = row.cells.length;
            td.innerHTML = '<div class="group-context-box"><div class="sd-empty">' +
                '<span class="sd-spin"></span>加载聊天记录</div></div>';
            tr.appendChild(td);
            row.parentNode.insertBefore(tr, row.nextSibling);
            button.setAttribute('aria-expanded', 'true');
            button.textContent = '收起上下文';
            var url = '/api/group/' + encodeURIComponent(roomId) + '/context?' + qs({
                msg_id: button.getAttribute('data-msg') || '',
                at: button.getAttribute('data-at') || '',
                minutes: button.getAttribute('data-min') || ''
            });
            fetch(url, { credentials: 'same-origin' }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (data) {
                    if (!response.ok) throw new Error((data && data.error) || '暂时取不到聊天记录，请稍后重试');
                    return data;
                });
            }).then(function (data) {
                td.innerHTML = contextHtml(data);
            }).catch(function (error) {
                td.innerHTML = '<div class="group-context-box"><div class="sd-err">' +
                    esc(error.message || '暂时取不到聊天记录，请稍后重试') + '</div></div>';
            });
        }

        function setMembersCount(box, value) {
            var text = value == null ? '—' : String(value);
            box.dataset.dsCount = text;
            var count = box.querySelector('.ds-collapse-count');
            if (count) count.textContent = '(' + text + ')';
        }

        function loadMembers() {
            var box = document.getElementById('group-members');
            if (!box || box.getAttribute('data-members-loaded') === '1') return;
            var panel = box.querySelector('.ds-collapse-panel');
            if (panel && panel.hidden) return;
            var body = box.querySelector('[data-members-body]');
            if (!body) return;
            box.setAttribute('data-members-loaded', '1');
            body.innerHTML = '<div class="group-context-box"><div class="sd-empty">' +
                '<span class="sd-spin"></span>加载群成员</div></div>';
            fetch('/api/group/' + encodeURIComponent(roomId) + '/members', { credentials: 'same-origin' })
                .then(function (response) {
                    return response.json().catch(function () { return {}; }).then(function (data) {
                        if (!response.ok) throw new Error((data && data.error) || '群成员加载失败，请稍后重试');
                        return data;
                    });
                })
                .then(function (data) {
                    body.innerHTML = membersHtml(data);
                    setMembersCount(box, (data.members || []).length);
                })
                .catch(function (error) {
                    box.setAttribute('data-members-loaded', '0');
                    body.innerHTML = '<div class="group-context-box"><div class="sd-err">' +
                        esc(error.message || '群成员加载失败，请稍后重试') + '</div></div>';
                    setMembersCount(box, null);
                });
        }

        document.addEventListener('click', function (event) {
            var button = event.target.closest ? event.target.closest('button[data-ctx]') : null;
            if (!button) return;
            event.preventDefault();
            toggleContext(button);
        });

        document.addEventListener('click', function (event) {
            var toggle = event.target.closest ? event.target.closest('#group-members .ds-collapse-toggle') : null;
            if (!toggle) return;
            setTimeout(loadMembers, 0);
        });

        function focusEvent() {
            var eventId = new URLSearchParams(location.search).get('event');
            if (!eventId) return;
            var rows = document.querySelectorAll('tr[data-msg]');
            var target = null;
            for (var i = 0; i < rows.length; i += 1) {
                if (rows[i].getAttribute('data-msg') === eventId) { target = rows[i]; break; }
            }
            if (!target) return;
            var collapse = target.closest('.ds-collapse');
            if (collapse) {
                var toggle = collapse.querySelector('.ds-collapse-toggle');
                if (toggle && toggle.getAttribute('aria-expanded') !== 'true') toggle.click();
            }
            target.classList.add('event-highlight');
            if (target.scrollIntoView) target.scrollIntoView({ block: 'center', behavior: 'smooth' });
            var button = target.querySelector('button[data-ctx]');
            if (button && button.getAttribute('aria-expanded') !== 'true') button.click();
        }

        setTimeout(focusEvent, 80);
        window.StcGroup = { context: toggleContext, members: loadMembers };
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
