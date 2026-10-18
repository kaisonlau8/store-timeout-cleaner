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
    let pollAttempts = 0, pollErrors = 0, pollStopped = false;
    window.addEventListener('pagehide', () => { pollStopped = true; clearTimeout(polling); });

    const today = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
    const days = (list) => (list.length === 7 ? '每天' : list.map((d) => '周' + DAY[d]).join('、'));
    const THIRTY_MINUTE_RULE_ID = '2055182790919555459';
    function isThirtyMinuteRule(group, minutes) {
        return String((group && group.rule_key) || '') === THIRTY_MINUTE_RULE_ID && Number(minutes) === 30;
    }
    const monthStart = () => today().slice(0, 7) + '-01';

    function ruleLogHref(ruleId) {
        const params = new URLSearchParams();
        params.set('rule_id', String(ruleId || ''));
        params.set('start', monthStart());
        params.set('end', today());
        return '/timeout?' + params.toString();
    }

    async function api(url, body) {
        const res = await fetch(url, body === undefined ? { credentials: 'same-origin', redirect: 'error', signal: AbortSignal.timeout(10000) } : {
            method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        let j = {};
        try { j = await res.json(); } catch (e) { j = {}; }
        if (!res.ok) { const error = new Error(j.error || ('请求失败 ' + res.status)); error.status = res.status; throw error; }
        return j;
    }

    function render(payload) {
        rules = payload.rules || [];
        const tbody = document.querySelector('#rules-table tbody');
        tbody.innerHTML = rules.length ? rules.map((g, i) => {
            const c = g.current;
            const logHref = ruleLogHref(c.rule_key);
            const hist = g.history.length > 1 ? '<div class="hist">历史版本：' + g.history.slice(1).map((h) =>
                esc(h.effective_from + ' ~ ' + (h.effective_to || '…') + ' · ' + h.minutes + ' 分钟 · ' + h.start_time + '-' + h.end_time + (h.enabled ? '' : ' · 停用'))).join('<br>') + '</div>' : '';
            return '<tr><td data-card-title><strong>' + esc(c.name) + '</strong>' + (c.note ? '<div class="hist ds-text-pretty">' + esc(c.note) + '</div>' : '') + hist + '</td>' +
                '<td><span class="ds-nowrap">' + esc(c.minutes) + ' 分钟</span></td><td>' + esc(days(c.days)) + '</td><td><span class="ds-nowrap">' + esc(c.start_time + ' - ' + c.end_time) + '</span></td>' +
                '<td><span class="ds-nowrap">' + esc(c.effective_from) + ' 起</span>' + (c.effective_to ? '<br>至 <span class="ds-nowrap">' + esc(c.effective_to) + '</span>' : '') + '</td>' +
                '<td>' + (c.enabled ? '<span class="ant-tag ant-tag-success">启用</span>' : '<span class="ant-tag">停用</span>') + '</td>' +
                '<td>' + (c.counts_for_kpi ? '<span class="ant-tag ant-tag-processing">计入 KPI</span>' : '<span class="ant-tag ant-tag-warning kpi-off">仅展示，不计 KPI</span>') + '</td>' +
                '<td><div class="ops"><a class="ant-btn ant-btn-sm" data-rule-log="' + i + '" href="' + esc(logHref) + '"><svg class="ai" aria-hidden="true"><use href="#i-right"/></svg>查看触发记录</a>' +
                '<button type="button" class="ant-btn ant-btn-sm" data-edit="' + i + '"><svg class="ai" aria-hidden="true"><use href="#i-edit"/></svg>编辑</button>' +
                '<button type="button" class="ant-btn ant-btn-sm" data-toggle="' + i + '">' + (c.enabled ? '停用' : '启用') + '</button></div></td></tr>';
        }).join('') : '<tr><td colspan="8" class="cl-muted" style="text-align:center">暂无规则</td></tr>';
        const a = payload.archive || {};
        $('archive-info').textContent = a.days ? '会话存档 ' + a.from + ' ~ ' + a.to + '（' + a.days + ' 天）' : '会话存档尚未拉取';
        const r = payload.last_run;
        if (r) {
            $('last-run-block').hidden = false;
            const card = (t, v, h) => '<div class="card"><h4>' + esc(t) + '</h4><div class="big-number" style="font-size:28px">' + esc(v) + '</div><div class="sub-stat">' + esc(h) + '</div></div>';
            $('last-run').innerHTML = card('区间', r.days + ' 天', r.start + ' ~ ' + r.end) + card('规则命中', r.total, '含同一消息多规则') +
                card('计入', r.valid, '有效超时消息') + card('规则过滤剔除', r.rule_excluded, '重复/话术/寒暄/@/连发') +
                card('DeepSeek 剔除', r.llm_excluded, '复核判定无需回复') + card('完成于', (r.finished_at || '').slice(5, 16), ({ cron: '定时同步', cli: '手动回填', restore: '手动回填' })[r.label] || r.label || '');
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
        if (job.status === 'running' && !polling && !pollStopped) poll();
    }

    function poll() {
        polling = setTimeout(async () => {
            if (pollStopped) return;
            pollAttempts += 1;
            try {
                const job = await api('/api/admin/job');
                pollErrors = 0;
                showJob(job);
                if (job.status !== 'running') {
                    clearTimeout(polling); polling = null;
                    if (job.status === 'done') {
                        const r = job.result || {};
                        FB.notification('success', '重算完成', r.start + ' ~ ' + r.end + '：计入 ' + r.valid + '，规则剔除 ' + r.rule_excluded + '，DeepSeek 剔除 ' + r.llm_excluded);
                    } else FB.notification('error', '重算失败', job.error || '');
                    load();
                    return;
                }
            } catch (e) {
                pollErrors += 1;
                if (e.status === 401 || e.status === 403) pollStopped = true;
            }
            polling = null;
            if (pollStopped || pollErrors >= 3 || pollAttempts >= 120) {
                pollStopped = true;
                FB.message('warning', '进度查询已暂停，请稍后手动刷新');
                return;
            }
            poll();
        }, 1500);
    }

    const form = $('rule-form');
    let kpiConfirmedKey = null;
    let modalSession = 0;
    let submitInFlight = false;
    function openModal(i) {
        modalSession += 1;
        if (!$('kpi-confirm').hidden) finishKpiConfirm(false);
        editing = i == null ? null : rules[i];
        kpiConfirmedKey = null;
        const c = editing ? editing.current : { name: '', minutes: 30, days: [1, 2, 3, 4, 5], start_time: '09:00', end_time: '20:00', enabled: true, counts_for_kpi: true, note: '' };
        $('rule-title').textContent = editing ? '编辑规则' : '新建规则';
        form.name.value = c.name; form.minutes.value = c.minutes;
        form.querySelectorAll('[name=days]').forEach((el) => { el.checked = c.days.indexOf(Number(el.value)) >= 0; });
        form.start.value = c.start_time; form.end.value = c.end_time;
        form.effective_from.value = editing ? today() : today();
        form.enabled.checked = !!c.enabled; form.counts_for_kpi.checked = c.counts_for_kpi !== false; form.note.value = c.note || '';
        $('rule-tip').hidden = !editing;
        $('rule-error').hidden = true;
        $('rule-mask').hidden = false; $('rule-modal').hidden = false;
        requestAnimationFrame(() => { $('rule-mask').classList.add('on'); $('rule-modal').classList.add('on'); });
        form.name.focus();
    }
    function closeModal() {
        modalSession += 1;
        if (!$('kpi-confirm').hidden) finishKpiConfirm(false);
        form.inert = false;
        form.removeAttribute('inert');
        const mask = $('rule-mask'), modal = $('rule-modal');
        mask.classList.remove('on'); modal.classList.remove('on');
        if (matchMedia('(prefers-reduced-motion: reduce)').matches) { mask.hidden = true; modal.hidden = true; return; }
        setTimeout(() => { mask.hidden = true; modal.hidden = true; }, 200);
    }

    let confirmResolve = null;
    function finishKpiConfirm(ok) {
        const mask = $('kpi-confirm-mask'), modal = $('kpi-confirm');
        mask.classList.remove('on'); modal.classList.remove('on');
        mask.hidden = true; modal.hidden = true;
        form.inert = false;
        form.removeAttribute('inert');
        const resolve = confirmResolve;
        confirmResolve = null;
        if (resolve) resolve(!!ok);
    }
    function confirmKpi() {
        if (confirmResolve) finishKpiConfirm(false);
        return new Promise((resolve) => {
            confirmResolve = resolve;
            form.inert = true;
            form.setAttribute('inert', '');
            $('kpi-confirm-mask').hidden = false;
            $('kpi-confirm').hidden = false;
            requestAnimationFrame(() => {
                $('kpi-confirm-mask').classList.add('on');
                $('kpi-confirm').classList.add('on');
            });
            $('kpi-confirm-cancel').focus();
        });
    }
    function kpiConfirmKey() {
        return String((editing && editing.rule_key) || '__new__') + '|' + Number(form.minutes.value);
    }
    async function confirmKpiIfNeeded() {
        if (!form.counts_for_kpi.checked) return true;
        if (isThirtyMinuteRule(editing, form.minutes.value)) return true;
        const key = kpiConfirmKey();
        if (kpiConfirmedKey === key) return true;
        const ok = await confirmKpi();
        if (ok) kpiConfirmedKey = key;
        return ok;
    }
    async function submit(body, key) {
        const res = await api('/api/admin/rules' + (key ? '/' + encodeURIComponent(key) : ''), body);
        FB.message('success', res.started ? '已保存，正在从 ' + res.recompute_from + ' 起重算' : '已保存；已有重算在进行，完成后请再点「全部重算」');
        await load();
        if (res.job) showJob(res.job);
    }
    form.counts_for_kpi.addEventListener('change', async () => {
        if (!form.counts_for_kpi.checked || isThirtyMinuteRule(editing, form.minutes.value)) return;
        const ok = await confirmKpiIfNeeded();
        if (!ok) form.counts_for_kpi.checked = false;
    });
    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        if (submitInFlight) return;
        const session = modalSession;
        submitInFlight = true;
        try {
            if (!(await confirmKpiIfNeeded())) {
                if (session !== modalSession) return;
                form.counts_for_kpi.checked = false;
                return;
            }
            if (session !== modalSession) return;
            const body = {
                name: form.name.value.trim(), minutes: Number(form.minutes.value),
                days: Array.from(form.querySelectorAll('[name=days]:checked')).map((el) => Number(el.value)),
                start: form.start.value, end: form.end.value, effective_from: form.effective_from.value,
                enabled: form.enabled.checked, counts_for_kpi: form.counts_for_kpi.checked, note: form.note.value.trim(),
            };
            const btn = $('rule-save');
            FB.setButtonLoading(btn, true);
            try { await submit(body, editing && editing.rule_key); closeModal(); }
            catch (e) { $('rule-error').hidden = false; $('rule-error').textContent = e.message; FB.message('error', e.message); }
            finally { FB.setButtonLoading(btn, false); }
        } finally {
            submitInFlight = false;
        }
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
                effective_from: today(), enabled: !c.enabled, counts_for_kpi: c.counts_for_kpi, note: c.note }, g.rule_key);
        } catch (e) { FB.message('error', e.message); }
        finally { FB.setButtonLoading(tg, false); }
    });
    $('btn-new').addEventListener('click', () => openModal(null));
    $('rule-x').addEventListener('click', closeModal);
    $('rule-cancel').addEventListener('click', closeModal);
    $('rule-mask').addEventListener('click', closeModal);
    $('kpi-confirm-mask').addEventListener('click', () => finishKpiConfirm(false));
    $('kpi-confirm-cancel').addEventListener('click', () => finishKpiConfirm(false));
    $('kpi-confirm-ok').addEventListener('click', () => finishKpiConfirm(true));
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (!$('kpi-confirm').hidden) finishKpiConfirm(false);
        else if (!$('rule-modal').hidden) closeModal();
    });
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
