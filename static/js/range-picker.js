/* Self-hosted, single-popup range selection. GET start/end remain the wire contract. */
(function () {
  'use strict';
  var pickers = [];
  var MIN = '2026-01-01';
  function iso(d) { return d.toISOString().slice(0, 10); }
  function day(s) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
    var d = new Date(s + 'T00:00:00Z');
    return !isNaN(d) && iso(d) === s ? d : null;
  }
  function error(a, b, today, optional) {
    if (optional && !a && !b) return '';
    a = a || (optional ? MIN : ''); b = b || (optional ? today : '');
    if (!day(a) || !day(b)) return '请输入完整日期，格式 YYYY-MM-DD';
    if (a > b) return '开始日期不能晚于结束日期';
    if (a < MIN || b > today) return '日期范围应在 ' + MIN + ' 至 ' + today + ' 之间';
    if ((day(b) - day(a)) / 86400000 + 1 > 366) return '日期跨度不能超过 366 天';
    return '';
  }
  function init() {
    document.querySelectorAll('[data-range-picker]').forEach(function (root) {
      var a = root.querySelector('[data-range-start]'), b = root.querySelector('[data-range-end]');
      var trigger = root.querySelector('.ant-picker-range'), text = root.querySelector('[data-range-text]');
      var explain = root.querySelector('.ant-form-item-explain-error');
      var today = root.dataset.today, optional = root.dataset.optional === '1';
      var popup = document.createElement('div');
      popup.className = 'stc-range-popup'; popup.hidden = true;
      popup.id = a.id + '-popup'; popup.setAttribute('role', 'dialog'); popup.setAttribute('aria-label', '选择日期范围');
      trigger.setAttribute('aria-controls', popup.id);
      popup.innerHTML = '<div class="stc-range-presets"></div><div class="stc-range-fields"><input type="text" inputmode="numeric" aria-label="开始日期" placeholder="YYYY-MM-DD"><span>→</span><input type="text" inputmode="numeric" aria-label="结束日期" placeholder="YYYY-MM-DD"></div><div class="stc-range-heading"><button type="button" data-year="-1" aria-label="上一年">«</button><button type="button" data-month="-1" aria-label="上个月">‹</button><strong></strong><button type="button" data-month="1" aria-label="下个月">›</button><button type="button" data-year="1" aria-label="下一年">»</button></div><div class="stc-range-grid"></div><div class="ant-form-item-explain-error" role="alert" hidden></div><div class="stc-range-footer"><span data-hint>先选开始日期，再选结束日期</span><button type="button" data-apply>确定</button></div>';
      document.body.appendChild(popup);
      var fields = popup.querySelectorAll('input'), grid = popup.querySelector('.stc-range-grid');
      var draftA, draftB, month, selectingEnd = false;
      function sync() { text.textContent = (a.value || '开始日期') + ' → ' + (b.value || '结束日期'); }
      function showError(msg, notify) {
        explain.textContent = msg; explain.hidden = !msg;
        trigger.setAttribute('aria-invalid', msg ? 'true' : 'false');
        var inner = popup.querySelector('.ant-form-item-explain-error'); inner.textContent = msg; inner.hidden = !msg;
        if (msg && notify && window.AntdFeedback) window.AntdFeedback.message('error', msg);
        return !msg;
      }
      function validate() { return showError(error(a.value, b.value, today, optional), true); }
      function close() { popup.hidden = true; trigger.setAttribute('aria-expanded', 'false'); }
      function draw() {
        fields[0].value = draftA; fields[1].value = draftB;
        var year = month.getUTCFullYear(), m = month.getUTCMonth();
        popup.querySelector('strong').textContent = year + ' 年 ' + (m + 1) + ' 月';
        grid.replaceChildren();
        ['一','二','三','四','五','六','日'].forEach(function (w) { var el=document.createElement('span'); el.textContent=w; grid.appendChild(el); });
        var offset = (month.getUTCDay() + 6) % 7;
        for (var i=0;i<offset;i++) grid.appendChild(document.createElement('span'));
        var count = new Date(Date.UTC(year,m+1,0)).getUTCDate();
        for (var n=1;n<=count;n++) {
          var value = iso(new Date(Date.UTC(year,m,n))), btn = document.createElement('button');
          btn.type='button'; btn.textContent=n; btn.dataset.date=value; btn.setAttribute('aria-label',value);
          btn.setAttribute('aria-pressed',String(value===draftA || value===draftB));
          if(value===today) btn.setAttribute('aria-current','date');
          btn.disabled=value<MIN || value>today;
          if (value === draftA || value === draftB) btn.className='selected';
          else if (value > draftA && value < draftB) btn.className='in-range';
          grid.appendChild(btn);
        }
        popup.querySelector('[data-hint]').textContent = selectingEnd ? '请选择结束日期' : '先选开始日期，再选结束日期';
      }
      var now = day(today), y = now.getUTCFullYear(), m = now.getUTCMonth();
      function monthRange(first, last) { return [iso(new Date(Date.UTC(y,first,1))), iso(new Date(Date.UTC(y,last+1,0)))]; }
      var previous = monthRange(m-1,m-1), quarter = Math.floor(m/3)*3;
      var recent = new Date(now); recent.setUTCDate(recent.getUTCDate()-29);
      var presets = [['本月',[iso(new Date(Date.UTC(y,m,1))),today]],['上月',previous],['本季度',monthRange(quarter,quarter+2)]];
      for (var q=0;q<4;q++) presets.push(['Q'+(q+1),monthRange(q*3,q*3+2)]);
      presets.push(['近 30 天',[iso(recent),today]]);
      presets.forEach(function (preset) {
        var btn=document.createElement('button'), dates=preset[1]; btn.type='button'; btn.textContent=preset[0];
        dates=[dates[0]<MIN?MIN:dates[0],dates[1]>today?today:dates[1]];
        btn.disabled=dates[0]>today || dates[1]<MIN;
        btn.title=preset[0]+'：'+dates.join(' ~ ')+'（当年，结束不晚于今天）';
        btn.addEventListener('click',function () { draftA=dates[0];draftB=dates[1];month=day(draftA.slice(0,7)+'-01');selectingEnd=false;showError('',false);draw(); });
        popup.querySelector('.stc-range-presets').appendChild(btn);
      });
      function position() {
        if (popup.hidden) return;
        var r=trigger.getBoundingClientRect();
        popup.style.left=Math.max(12,Math.min(r.left,window.innerWidth-popup.offsetWidth-12))+'px';
        popup.style.top=Math.max(12,Math.min(r.bottom+5,window.innerHeight-popup.offsetHeight-12))+'px';
      }
      trigger.addEventListener('click',function () {
        if (!popup.hidden) { close();return; }
        pickers.forEach(function (p) { p.close(); });
        draftA=a.value;draftB=b.value;selectingEnd=false;month=day((day(draftA)?draftA:today).slice(0,7)+'-01');
        draw();popup.hidden=false;trigger.setAttribute('aria-expanded','true');position();fields[0].focus();
      });
      popup.addEventListener('click',function (ev) {
        var btn=ev.target.closest('button');if(!btn) return;
        if(btn.dataset.month || btn.dataset.year) { month.setUTCMonth(month.getUTCMonth()+Number(btn.dataset.month || 0)+12*Number(btn.dataset.year || 0));draw(); }
        if(btn.dataset.date) {
          if(!selectingEnd || btn.dataset.date < draftA) {draftA=btn.dataset.date;draftB='';selectingEnd=true;}
          else {draftB=btn.dataset.date;selectingEnd=false;}
          draw();
        }
        if(btn.hasAttribute('data-apply')) {
          draftA=fields[0].value.trim();draftB=fields[1].value.trim();
          if(!showError(error(draftA,draftB,today,optional),true)) return;
          a.value=draftA;b.value=draftB;sync();
          a.dispatchEvent(new Event('change',{bubbles:true}));b.dispatchEvent(new Event('change',{bubbles:true}));
          close();trigger.focus();
        }
      });
      fields.forEach(function (field) { field.addEventListener('change',function () {draftA=fields[0].value.trim();draftB=fields[1].value.trim();showError(error(draftA,draftB,today,optional),false);draw();}); });
      popup.addEventListener('keydown',function (ev) { if(ev.key==='Escape'){close();trigger.focus();} if(ev.key==='Enter' && ev.target.tagName==='INPUT'){ev.preventDefault();popup.querySelector('[data-apply]').click();} });
      document.addEventListener('pointerdown',function(ev){if(!root.contains(ev.target)&&!popup.contains(ev.target)) close();});
      window.addEventListener('resize',position);window.addEventListener('scroll',position,true);
      var form=root.closest('form');if(form) {
        form.addEventListener('submit',function(ev){if(!validate()){ev.preventDefault();ev.stopImmediatePropagation();}},true);
        var details=form.closest('details[data-mobile-collapsed]');
        if(details && !form.dataset.rangeDirtyReady) {
          form.dataset.rangeDirtyReady='1';
          function dirty() { var summary=details.querySelector('summary'); if(!summary.querySelector('[data-range-dirty]')) { var note=document.createElement('span');note.dataset.rangeDirty='1';note.textContent=' · 有未应用修改';summary.appendChild(note); } }
          form.addEventListener('input',dirty);form.addEventListener('change',dirty);
        }
      }
      pickers.push({validate:validate,sync:sync,close:close});sync();
    });
  }
  window.StcRangePicker = {validateAll:function(){return pickers.map(function(p){return p.validate();}).every(Boolean);},sync:function(){pickers.forEach(function(p){p.sync();});},error:error};
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',init);else init();
})();
