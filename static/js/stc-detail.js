// 不计 KPI 的规则名来自导航上的 data-non-kpi（/admin/rules 的「计入 KPI」开关）
window.stcKpiTag = function (name) {
    var nav = document.getElementById('site-nav'); var list = [];
    try { list = JSON.parse((nav && nav.getAttribute('data-non-kpi')) || '[]'); } catch (e) { list = []; }
    return String(name || '').split(' / ').some(function (n) { return list.indexOf(n.trim()) >= 0; })
        ? ' <span class="ant-tag ant-tag-warning kpi-off" title="该规则只在超时机器人中展示，不计入 KPI/评分">仅展示，不计 KPI</span>' : '';
};
/* 超时明细下钻：StcDetail.events(params,title) / members(roomId,name) */
(function () {
    'use strict';
    var mask, drawer, titleEl, bodyEl, backBtn, stack = [];
    function esc(t) { return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
    function ensure() {
        if (drawer) return;
        mask = document.createElement('div'); mask.className = 'sd-mask'; mask.hidden = true;
        drawer = document.createElement('aside'); drawer.className = 'sd-drawer'; drawer.hidden = true;
        drawer.setAttribute('role', 'dialog'); drawer.setAttribute('aria-modal', 'true');
        drawer.innerHTML = '<div class="sd-head"><button type="button" class="sd-back" aria-label="返回" hidden><svg class="ai" aria-hidden="true"><use href="#i-arrow-left"/></svg></button><h3></h3><button type="button" class="sd-x ds-close" aria-label="关闭"><svg class="ai" aria-hidden="true"><use href="#i-close"/></svg></button></div><div class="sd-body"></div>';
        document.body.appendChild(mask); document.body.appendChild(drawer);
        titleEl = drawer.querySelector('h3'); bodyEl = drawer.querySelector('.sd-body'); backBtn = drawer.querySelector('.sd-back');
        mask.addEventListener('click', close);
        drawer.querySelector('.sd-x').addEventListener('click', close);
        backBtn.addEventListener('click', function () { stack.pop(); var prev = stack.pop(); if (prev) prev(); });
        document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !drawer.hidden) close(); });
        bodyEl.addEventListener('click', onBodyClick);
    }
    function open(title, render) {
        ensure(); stack.push(render);
        backBtn.hidden = stack.length < 2;
        titleEl.textContent = title;
        mask.hidden = false; drawer.hidden = false; document.body.classList.add('sd-lock');
        requestAnimationFrame(function () { mask.classList.add('on'); drawer.classList.add('on'); });
        bodyEl.scrollTop = 0;
    }
    function close() {
        if (!drawer) return; stack = [];
        mask.classList.remove('on'); drawer.classList.remove('on'); document.body.classList.remove('sd-lock');
        setTimeout(function () { mask.hidden = true; drawer.hidden = true; }, 250);
    }
    function loading() { bodyEl.innerHTML = '<div class="ds-skeleton-block" style="height:16px;margin:12px 0;width:60%"></div><div class="ds-skeleton-block" style="height:16px;margin:12px 0"></div><div class="ds-skeleton-block" style="height:16px;margin:12px 0;width:82%"></div>'; }
    function errorText(status) {
        if (status === 400) return '参数无效，无法查询';
        if (status === 403) return '没有权限查看';
        if (status === 404) return '没有找到对应记录';
        if (status === 0 || status >= 500) return '服务暂时不可用，请稍后重试';
        return '请求失败，请稍后重试';
    }
    function getJSON(url) {
        return fetch(url, { credentials: 'same-origin' }).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (j) {
                if (!r.ok) throw new Error((j && j.error) || errorText(r.status));
                return j;
            });
        }, function () {
            throw new Error('服务暂时不可用，请稍后重试');
        });
    }
    function qs(o) { return Object.keys(o).filter(function (k) { return o[k] !== '' && o[k] != null; }).map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(o[k]); }).join('&'); }

    function excludeTag(reason) {
        if (reason === '重复触发(同一消息)') return '<span class="sd-tag">同一消息另有规则命中</span>';
        return '<span class="sd-tag">不计入：' + esc(reason) + '</span>';
    }
    function groupHref(roomId, msgId, day) {
        var date = String(day || '').slice(0, 10);
        return '/group/' + encodeURIComponent(roomId) + '?event=' + encodeURIComponent(msgId || '') +
            '&start=' + encodeURIComponent(date) + '&end=' + encodeURIComponent(date);
    }
    function eventItem(e) {
        return '<li class="sd-item" data-room="' + esc(e.room_id) + '" data-at="' + esc(e.created_at) + '" data-min="' + esc(e.minutes || 30) + '" data-msg="' + esc(e.msg_id) + '" data-name="' + esc(e.group_name) + '">' +
            '<div class="sd-row"><a class="sd-link sd-title" href="' + esc(groupHref(e.room_id, e.msg_id, e.created_at)) + '">' + esc(e.group_name || e.room_id) + '<svg class="ai" aria-hidden="true"><use href="#i-right"/></svg></a></div>' +
            '<div class="sd-row"><span>' + esc(e.created_at) + '</span><span class="sd-tag warn">' + esc(e.rule_name || '超时') + '</span>' + (window.stcKpiTag ? window.stcKpiTag(e.rule_name) : '') +
            (e.minutes ? '<span>超过 ' + esc(e.minutes) + ' 分钟未回复</span>' : '') +
            (e.exclude_reason ? excludeTag(e.exclude_reason) : '') + '</div>' +
            (e.content ? '<div class="sd-row sd-quote">' + esc(e.content) + '</div>' : '') +
            '<div class="sd-row"><span>' + esc(e.store || '未归店') + (e.theater ? ' · ' + esc(e.theater) : '') + '</span>' +
            '<button type="button" class="sd-btn" data-act="ctx" aria-expanded="false" style="margin-left:auto">查看聊天上下文</button></div>' +
            '<div class="sd-ctx" hidden></div></li>';
    }
    function statusLabel(status) {
        return status === 'excluded' ? '已剔除 ' : (status === 'all' ? '共 ' : '计入 ');
    }
    function events(params, title) {
        var state = { offset: 0, items: [] };
        var render = function () {
            open(title || '超时明细', render);
            loading(); state.offset = 0; state.items = [];
            page();
        };
        function page() {
            var p = Object.assign({}, params, { limit: 100, offset: state.offset });
            var label = statusLabel(params.status);
            getJSON('/api/timeout/events?' + qs(p)).then(function (j) {
                state.items = state.items.concat(j.events || []); state.offset = state.items.length;
                if (!state.items.length) { bodyEl.innerHTML = '<div class="sd-empty">当前条件下暂无超时记录</div>'; return; }
                bodyEl.innerHTML = '<p class="sd-sub">' + label + j.total + ' 条，按时间倒序。点群名打开群详情，点「查看聊天上下文」看前后消息。</p><ul class="sd-list">' +
                    state.items.map(eventItem).join('') + '</ul>' +
                    (state.items.length < j.total ? '<div class="sd-more"><button type="button" class="sd-btn" data-act="more">加载更多（剩 ' + (j.total - state.items.length) + '）</button></div>' : '');
                bodyEl._more = page;
            }).catch(function (e) { bodyEl.innerHTML = '<div class="sd-err">' + esc(e.message) + '</div>'; });
        }
        render();
    }
    function members(roomId, name) {
        var render = function () {
            open((name || '群') + ' · 群成员', render); loading();
            var detailLink = '<p class="sd-sub"><a class="sd-link" href="/group/' + encodeURIComponent(roomId) + '">打开群详情<svg class="ai" aria-hidden="true"><use href="#i-right"/></svg></a></p>';
            getJSON('/api/group/' + encodeURIComponent(roomId) + '/members').then(function (j) {
                var list = j.members || [];
                if (!list.length) { bodyEl.innerHTML = detailLink + '<div class="sd-empty">SCRM 未返回该群成员</div>'; return; }
                var cnt = { staff: 0, customer: 0, robot: 0 };
                list.forEach(function (m) { cnt[m.role]++; });
                bodyEl.innerHTML = detailLink + '<p class="sd-sub">共 ' + list.length + ' 人：员工 ' + cnt.staff + ' · 客户 ' + cnt.customer + ' · 机器人 ' + cnt.robot + '（实时取自 SCRM，缓存 10 分钟）</p><ul class="sd-list">' +
                    list.map(function (m) {
                        return '<li class="sd-item"><div class="sd-row"><span class="sd-title">' + esc(m.name) + '</span><span class="sd-tag ' + esc(m.role) + '">' + esc(m.role_label) + '</span>' +
                            (m.nick && m.nick !== m.name ? '<span>群昵称 ' + esc(m.nick) + '</span>' : '') + '</div>' +
                            '<div class="sd-row"><span>入群 ' + esc(m.join_time || '未知') + '</span>' + (m.join_scene ? '<span>' + esc(m.join_scene) + '</span>' : '') + '</div></li>';
                    }).join('') + '</ul>';
            }).catch(function (e) { bodyEl.innerHTML = detailLink + '<div class="sd-err">' + esc(e.message) + '</div>'; });
        };
        render();
    }
    function loadCtx(li, btn) {
        var box = li.querySelector('.sd-ctx');
        if (!box.hidden) { box.hidden = true; btn.setAttribute('aria-expanded', 'false'); btn.textContent = '查看聊天上下文'; return; }
        box.hidden = false; btn.setAttribute('aria-expanded', 'true'); btn.textContent = '收起上下文';
        if (box.dataset.done) return;
        box.innerHTML = '<div class="sd-empty"><span class="sd-spin"></span>加载中</div>';
        getJSON('/api/timeout/context?' + qs({ room_id: li.dataset.room, at: li.dataset.at, minutes: li.dataset.min, msg_id: li.dataset.msg })).then(function (j) {
            box.dataset.done = '1';
            var msgs = j.messages || [];
            if (!msgs.length) { box.innerHTML = '<div class="sd-empty">会话存档在 ' + esc((j.window || []).join(' ~ ')) + ' 内无消息</div>'; return; }
            box.innerHTML = '<p class="sd-sub">' + esc((j.window || []).join(' ~ ')) + (j.exact ? '；橙色为触发超时的客户消息' : '；橙色为超时前最后一条客户消息（推算）') + '</p>' + msgs.map(function (m) {
                return '<div class="sd-msg role-' + esc(m.role) + (m.trigger ? ' trigger' : '') + '"><span class="who">' + esc(m.sender) + '</span><span class="when">' + esc(m.time.slice(11)) + '</span><div class="txt">' + esc(m.text) + '</div></div>';
            }).join('');
        }).catch(function (e) { box.innerHTML = '<div class="sd-err">' + esc(e.message) + '</div>'; });
    }
    function onBodyClick(e) {
        var t = e.target.closest('[data-act]'); if (!t) return;
        var li = t.closest('.sd-item');
        if (t.dataset.act === 'members') members(li.dataset.room, li.dataset.name);
        else if (t.dataset.act === 'ctx') loadCtx(li, t);
        else if (t.dataset.act === 'more' && bodyEl._more) { t.disabled = true; t.innerHTML = '<span class="sd-spin"></span>加载中'; bodyEl._more(); }
    }
    // 页面上的声明式入口：data-sd-members="roomId" data-sd-name / data-sd-events='{"store":..}'
    document.addEventListener('click', function (e) {
        var m = e.target.closest('[data-sd-members]');
        if (m) { e.preventDefault(); members(m.getAttribute('data-sd-members'), m.getAttribute('data-sd-name') || m.textContent.trim()); return; }
        var ev = e.target.closest('[data-sd-events]');
        if (ev) { e.preventDefault(); events(JSON.parse(ev.getAttribute('data-sd-events')), ev.getAttribute('data-sd-title') || '超时明细'); }
    });
    document.addEventListener('keydown', function (e) {
        if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('[data-sd-events][role=button]')) { e.preventDefault(); e.target.click(); }
    });
    window.StcDetail = { events: events, members: members, close: close };
})();
