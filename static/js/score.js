(function () {
  var box = document.getElementById("llm-status");
  if (!box || box.dataset.auto !== "1") return;
  if (box.dataset.state !== "running") return;

  function query() {
    return "start=" + encodeURIComponent(box.dataset.start || "") +
      "&end=" + encodeURIComponent(box.dataset.end || "");
  }

  var attempts = 0, errors = 0, stopped = false;
  var reloadKey = "llm-auto-reload:" + location.pathname + ":" + (box.dataset.start || "") + ":" + (box.dataset.end || "");
  var initialToken = box.dataset.since || "";
  var timer;
  function stop() { stopped = true; clearTimeout(timer); }
  window.addEventListener('pagehide', stop);
  window.addEventListener('beforeunload', stop);
  // A late response from the previous range must not cancel a user's GET submit.
  document.addEventListener('submit', stop, true);
  function later(delay) {
    if (stopped || attempts >= 120 || errors >= 3) {
      box.textContent = '复核进度查询已暂停，请稍后手动刷新。';
      return;
    }
    timer = setTimeout(poll, delay);
  }

  function poll() {
    if (stopped) return;
    attempts += 1;
    fetch("/api/score/llm-status?" + query(), {redirect: 'error', signal: AbortSignal.timeout(10000)})
      .then(function (response) { if (!response.ok) { stopped = response.status === 401 || response.status === 403; throw new Error('status'); } return response.json(); })
      .then(function (data) {
        if (!data || stopped) return;
        errors = 0;
        if (data.status === "running") {
          box.textContent = "LLM复核已自动计入。正在补齐 " + (data.done || 0) + "/" + (data.total || 0);
          later(1500);
          return;
        }
        var finished = data.status === "done" || data.status === "error";
        if (!finished) return;
        stopped = true;
        try {
          if (sessionStorage.getItem(reloadKey) === initialToken) return;
          sessionStorage.setItem(reloadKey, initialToken);
        } catch (err) {}
        window.location.reload();
      })
      .catch(function () {
        errors += 1;
        later(2000);
      });
  }

  poll();
})();

(function () {
  var input = document.getElementById("detail-q");
  if (!input) return;

  function apply() {
    var q = input.value.trim().toLowerCase();
    document.querySelectorAll(".store-table tbody tr").forEach(function (tr) {
      if (tr.querySelector("td[colspan]")) return;
      tr.hidden = !!(q && tr.textContent.toLowerCase().indexOf(q) < 0);
    });
  }

  input.addEventListener("input", apply);
  input.addEventListener("change", function () {
    var params = new URLSearchParams(location.search);
    if (input.value.trim()) params.set("dq", input.value.trim());
    else params.delete("dq");
    var form = document.querySelector('form[action="/score/store"]');
    if (form) {
      var hidden = form.querySelector('input[name="dq"]');
      if (!hidden) { hidden = document.createElement('input'); hidden.type = 'hidden'; hidden.name = 'dq'; form.appendChild(hidden); }
      hidden.value = input.value.trim();
    }
    var qs = params.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  });
  if (input.value) apply();
})();
