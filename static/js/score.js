(function () {
  var btn = document.getElementById("llm-run");
  var box = document.getElementById("llm-status");
  if (!btn || !box) return;

  function query() {
    return "start=" + encodeURIComponent(btn.dataset.start || "") +
      "&end=" + encodeURIComponent(btn.dataset.end || "");
  }

  function paint(data) {
    if (!data) return;
    if (data.status === "running") {
      box.textContent = "正在复核 " + (data.done || 0) + "/" + (data.total || 0);
      return;
    }
    if (data.status === "done") {
      box.textContent = "复核完成" + (data.failed ? "，失败 " + data.failed + " 批，未返回的消息仍计入" : "");
      return;
    }
    if (data.status === "error") {
      box.textContent = data.note || "复核失败";
    }
  }

  function poll() {
    fetch("/api/score/llm-status?" + query())
      .then(function (response) { return response.json(); })
      .then(function (data) {
        paint(data);
        if (data.status === "running") {
          setTimeout(poll, 1000);
          return;
        }
        if (data.status === "done" && btn.dataset.started === "1") {
          window.location.href = "/score?" + query() + "&llm=1";
        }
      })
      .catch(function () {
        box.textContent = "复核状态获取失败";
      });
  }

  btn.addEventListener("click", function () {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.dataset.started = "1";
    box.textContent = "正在启动复核…";
    fetch("/api/score/llm-review?" + query() + "&force=" + (btn.dataset.force || "0"), { method: "POST" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        paint(data);
        if (data.status === "off") {
          box.textContent = "未配置 DEEPSEEK_API_KEY，LLM 复核关闭";
          return;
        }
        poll();
      })
      .catch(function () {
        box.textContent = "复核启动失败";
        btn.disabled = false;
      });
  });

  if (btn.dataset.running === "1") poll();
})();
