/* 超时计算规则管理：列表 / 新建 / 编辑（带生效日的版本）/ 后台重算进度 */
document.addEventListener('DOMContentLoaded', () => {
    'use strict';
    const FB = window.AntdFeedback;
    const DAY = ['', '一', '二', '三', '四', '五', '六', '日'];
    const $ = (id) => document.getElementById(id);
    const esc = (t) => FB.escapeHtml(String(t == null ? '' : t));
    let rules = [];
    let editing = null;
    let polling = null;

    const today = () => { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); };
    const days = (list) => (list.length === 7 ? '每天' : list.map((d) => '周' + DAY[d]).join('、'));

    async function api(url, body) {
        const res = await fetch(url, body === undefined ? { credentials: 'same-origin' } : {
            method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        let j = {};
        try { j = await res.json(); } catch (e) { j = {}; }
        if (!res.ok) throw new Error(j.error || ('请求失败 ' + res.status));
        return j;
    }

    function render(payload) {
        rules = payload.rules || [];
        const tbody = document.querySelector('#rules-table tbody');
        tbody.innerHTML = rules.length ? rules.map((g, i) => {
            const c = g.current;
            const hist = g.history.length > 1 ? '<div class="hist">历史版本：' + g.history.slice(1).map((h) =>
                esc(h.effective_from + ' ~ ' + (h.effective_to || '…') + ' · ' + h.minutes + ' 分钟 · ' + h.start_time + '-' + h.end_time + (h.enabled ? '' : ' · 停用'))).join('<br>') + '</div>' : '';
            return '<tr><td data-card-title><strong>' + esc(c.name) + '</strong>' + (c.note ? '<div class="hist">' + esc(c.note) + '</div>' : '') + hist + '</td>' +
                '<td>' + esc(c.minutes) + ' 分钟</td><td>' + esc(days(c.days)) + '</td><td>' + esc(c.start_time + ' - ' + c.end_time) + '</td>' +
                '<td>' + esc(c.effective_from) + ' 起' + (c.effective_to ? '<br>至 ' + esc(c.effective_to) : '') + '</td>' +
                '<td>' + (c.enabled ? '<span class="ant-tag ant-tag-success">启用</span>' : '<span class="ant-tag">停用</span>') + '</td>' +
                '<td><div class="ops"><button type="button" class="ant-btn ant-btn-sm" data-edit="' + i + '"><svg class="ai" aria-hidden="true"><use href="#i-edit"/></svg>编辑</button>' +
                '<button type="button" class="ant-btn ant-btn-sm" data-toggle="' + i + '">' + (c.enabled ? '停用' : '启用') + '</button></div></td></tr>';
        }).join('') : '<tr><td colspan="7" class="cl-muted" style="text-align:center">暂无规则</td></tr>';
        const a = payload.archive || {};
        $('archive-info').textContent = a.days ? '会话存档 ' + a.from + ' ~ ' + a.to + '（' + a.days + ' 天）' : '会话存档尚未拉取';
        const r = payload.last_run;
        if (r) {
            $('last-run-block').hidden = false;
            const card = (t, v, h) => '<div class="card"><h4>' + esc(t) + '</h4><div class="big-number" style="font-size:28px">' + esc(v) + '</div><div class="sub-stat">' + esc(h) + '</div></div>';
            $('last-run').innerHTML = card('区间', r.days + ' 天', r.start + ' ~ ' + r.end) + card('规则命中', r.total, '含同一消息多规则') +
                card('计入', r.valid, '有效超时消息') + card('规则过滤剔除', r.rule_excluded, '重复/话术/寒暄/@/连发') +
                card('DeepSeek 剔除', r.llm_excluded, '复核判定无需回复') + card('完成于', (r.finished_at || '').slice(5, 16), r.label || '');
        }
        showJob(payload.job);
    }

    async function load() {
        try { render(await api('/api/admin/rules')); } catch (e) { FB.message('error', e.message); }
    }

    function showJob(job) {
        if (!job || job.status === 'idle') { $('job-block').hidden = true; return; }
        const pct = job.total ? Math.round((job.done / job.total) * 100) : (job.status === 'done' ? 100 : 0);
        $('job-block').hidden = job.status === 'done' && !polling;
        $('job-phase').textContent = (job.label ? job.label + '：' : '') + (job.status === 'error' ? '失败 ' + (job.error || '') : job.phase || '');
        $('job-count').textContent = job.total ? job.done + ' / ' + job.total + ' 天' : '';
        $('job-bg').style.width = pct + '%';
        $('job-text').textContent = pct + '%';
        const bar = $('job-bar');
        bar.classList.remove('ant-progress-status-active', 'ant-progress-status-success', 'ant-progress-status-exception');
        bar.classList.add(job.status === 'error' ? 'ant-progress-status-exception' : job.status === 'done' ? 'ant-progress-status-success' : 'ant-progress-status-active');
        if (job.status === 'running' && !polling) poll();
    }

    function poll() {
        polling = setInterval(async () => {
            try {
                const job = await api('/api/admin/job');
                showJob(job);
                if (job.status !== 'running') {
                    clearInterval(polling); polling = null;
                    if (job.status === 'done') {
                        const r = job.result || {};
                        FB.notification('success', '重算完成', r.start + ' ~ ' + r.end + '：计入 ' + r.valid + '，规则剔除 ' + r.rule_excluded + '，DeepSeek 剔除 ' + r.llm_excluded);
                    } else FB.notification('error', '重算失败', job.error || '');
                    load();
                }
            } catch (e) { /* 继续轮询 */ }
        }, 1500);
    }

    const form = $('rule-form');
    function openModal(i) {
        editing = i == null ? null : rules[i];
        const c = editing ? editing.current : { name: '', minutes: 30, days: [1, 2, 3, 4, 5], start_time: '09:00', end_time: '20:00', enabled: true, note: '' };
        $('rule-title').textContent = editing ? '编辑规则' : '新建规则';
        form.name.value = c.name; form.minutes.value = c.minutes;
        form.querySelectorAll('[name=days]').forEach((el) => { el.checked = c.days.indexOf(Number(el.value)) >= 0; });
        form.start.value = c.start_time; form.end.value = c.end_time;
        form.effective_from.value = editing ? today() : today();
        form.enabled.checked = !!c.enabled; form.note.value = c.note || '';
        $('rule-tip').hidden = !editing;
        $('rule-error').hidden = true;
        $('rule-mask').hidden = false; $('rule-modal').hidden = false;
        form.name.focus();
    }
    function closeModal() { $('rule-mask').hidden = true; $('rule-modal').hidden = true; }
    async function submit(body, key) {
        const res = await api('/api/admin/rules' + (key ? '/' + encodeURIComponent(key) : ''), body);
        FB.message('success', res.started ? '已保存，正在从 ' + res.recompute_from + ' 起重算' : '已保存；已有重算在进行，完成后请再点「全部重算」');
        await load();
        if (res.job) showJob(res.job);
    }
    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const body = {
            name: form.name.value.trim(), minutes: Number(form.minutes.value),
            days: Array.from(form.querySelectorAll('[name=days]:checked')).map((el) => Number(el.value)),
            start: form.start.value, end: form.end.value, effective_from: form.effective_from.value,
            enabled: form.enabled.checked, note: form.note.value.trim(),
        };
        const btn = $('rule-save');
        FB.setButtonLoading(btn, true);
        try { await submit(body, editing && editing.rule_key); closeModal(); }
        catch (e) { $('rule-error').hidden = false; $('rule-error').textContent = e.message; FB.message('error', e.message); }
        finally { FB.setButtonLoading(btn, false); }
    });
    document.querySelector('#rules-table').addEventListener('click', async (ev) => {
        const ed = ev.target.closest('[data-edit]');
        if (ed) { openModal(Number(ed.dataset.edit)); return; }
        const tg = ev.target.closest('[data-toggle]');
        if (!tg) return;
        const g = rules[Number(tg.dataset.toggle)]; const c = g.current;
        FB.setButtonLoading(tg, true);
        try {
            await submit({ name: c.name, minutes: c.minutes, days: c.days, start: c.start_time, end: c.end_time,
                effective_from: today(), enabled: !c.enabled, note: c.note }, g.rule_key);
        } catch (e) { FB.message('error', e.message); }
        finally { FB.setButtonLoading(tg, false); }
    });
    $('btn-new').addEventListener('click', () => openModal(null));
    $('rule-x').addEventListener('click', closeModal);
    $('rule-cancel').addEventListener('click', closeModal);
    $('rule-mask').addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('rule-modal').hidden) closeModal(); });
    $('btn-recompute').addEventListener('click', async () => {
        const btn = $('btn-recompute');
        FB.setButtonLoading(btn, true);
        try {
            const r = await api('/api/admin/recompute', {});
            FB.message(r.started ? 'info' : 'warning', r.started ? '已开始全部重算' : '已有重算在进行');
            showJob(r.job);
        } catch (e) { FB.message('error', e.message); }
        finally { FB.setButtonLoading(btn, false); }
    });
    load();
});
