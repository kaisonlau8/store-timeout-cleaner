(function () {
  var box = document.getElementById("llm-status");
  if (!box || box.dataset.auto !== "1") return;

  function query() {
    return "start=" + encodeURIComponent(box.dataset.start || "") +
      "&end=" + encodeURIComponent(box.dataset.end || "");
  }

  var reloadKey = "llm-auto-reload:" + (box.dataset.start || "") + ":" + (box.dataset.end || "") + ":" + (box.dataset.reload || "");

  function poll() {
    fetch("/api/score/llm-status?" + query())
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!data) return;
        if (data.status === "running") {
          box.textContent = "LLM复核已自动计入。正在补齐 " + (data.done || 0) + "/" + (data.total || 0);
          setTimeout(poll, 1000);
          return;
        }
        var finished = data.status === "done" || data.status === "error";
        var changed = data.status !== (box.dataset.state || "") ||
          (data.updated_at || "") !== (box.dataset.since || "");
        if (!finished || !changed) return;
        var token = (data.status || "") + "|" + (data.updated_at || "");
        try {
          if (sessionStorage.getItem(reloadKey) === token) return;
          sessionStorage.setItem(reloadKey, token);
        } catch (err) {}
        window.location.href = box.dataset.reload || ("/score?" + query());
      })
      .catch(function () {
        setTimeout(poll, 2000);
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
    var qs = params.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  });
  if (input.value) apply();
})();
