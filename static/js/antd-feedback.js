/* Ant Design 交互反馈：Button loading、Spin、Upload（XHR 进度）、Message、Notification。 */
(function () {
  var ICONS = {
    success: "i-check-circle",
    error: "i-close-circle",
    warning: "i-exclamation-circle",
    info: "i-info-circle",
    loading: "i-reload",
    close: "i-close",
  };

  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function iconSvg(name, extraClass) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "ai" + (extraClass ? " " + extraClass : ""));
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#" + (ICONS[name] || ICONS.info));
    svg.appendChild(use);
    return svg;
  }

  function ensureHolder(id, className, live) {
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement("div");
      el.id = id;
      el.className = className;
      el.setAttribute("aria-live", live || "polite");
      document.body.appendChild(el);
    }
    return el;
  }

  function removeLater(el, ms) {
    window.setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, ms);
  }

  function message(type, content, duration) {
    var kind = ICONS[type] ? type : "info";
    var holder = ensureHolder("ant-message-holder", "ant-message");
    var notice = document.createElement("div");
    notice.className = "ant-message-notice";
    notice.setAttribute("role", kind === "error" ? "alert" : "status");
    var contentWrap = document.createElement("div");
    contentWrap.className = "ant-message-notice-content";
    var row = document.createElement("div");
    row.className = "ant-message-custom-content ant-message-" + kind;
    row.appendChild(iconSvg(kind));
    var text = document.createElement("span");
    text.textContent = content == null ? "" : String(content);
    row.appendChild(text);
    contentWrap.appendChild(row);
    notice.appendChild(contentWrap);
    holder.appendChild(notice);
    removeLater(notice, duration == null ? 2500 : duration);
  }

  function notification(type, title, description, duration) {
    var kind = ICONS[type] ? type : "info";
    var holder = ensureHolder("ant-notification-holder", "ant-notification");
    var notice = document.createElement("div");
    notice.className = "ant-notification-notice ant-notification-notice-" + kind;
    notice.setAttribute("role", kind === "error" ? "alert" : "status");

    var icon = document.createElement("div");
    icon.className = "ant-notification-notice-icon";
    icon.appendChild(iconSvg(kind));

    var body = document.createElement("div");
    body.className = "ant-notification-notice-content";
    var titleEl = document.createElement("div");
    titleEl.className = "ant-notification-notice-message";
    titleEl.textContent = title == null ? "" : String(title);
    var descEl = document.createElement("div");
    descEl.className = "ant-notification-notice-description";
    descEl.textContent = description == null ? "" : String(description);
    body.appendChild(titleEl);
    body.appendChild(descEl);

    var close = document.createElement("button");
    close.type = "button";
    close.className = "ant-notification-notice-close";
    close.setAttribute("aria-label", "关闭");
    close.appendChild(iconSvg("close"));
    close.addEventListener("click", function () {
      if (notice.parentNode) notice.parentNode.removeChild(notice);
    });

    notice.appendChild(icon);
    notice.appendChild(body);
    notice.appendChild(close);
    holder.appendChild(notice);
    removeLater(notice, duration == null ? 4500 : duration);
  }

  function setButtonLoading(el, loading, text) {
    if (!el) return;
    var on = !!loading;
    if ((el.dataset.antdLoading === "1") === on) return;
    el.dataset.antdLoading = on ? "1" : "0";
    el.classList.toggle("ant-btn-loading", on);
    el.setAttribute("aria-busy", on ? "true" : "false");
    if (on) el.setAttribute("aria-disabled", "true");
    else el.removeAttribute("aria-disabled");
    if (el.tagName === "BUTTON") el.disabled = on;

    var label = el.querySelector("[data-btn-label]");
    if (label) {
      if (!label.dataset.idleText) label.dataset.idleText = label.textContent;
      if (on) {
        label.textContent = text != null ? String(text) : (el.dataset.loadingText || label.dataset.idleText);
      } else {
        label.textContent = label.dataset.idleText;
      }
    }

    var spin = el.querySelector(":scope > .ant-btn-loading-icon");
    if (on) {
      if (!spin) {
        spin = iconSvg("loading", "ant-btn-loading-icon");
        el.insertBefore(spin, el.firstChild);
      }
      Array.prototype.forEach.call(el.querySelectorAll(":scope > .ai:not(.ant-btn-loading-icon)"), function (node) {
        node.setAttribute("hidden", "");
      });
    } else if (spin) {
      spin.parentNode.removeChild(spin);
      Array.prototype.forEach.call(el.querySelectorAll(":scope > .ai"), function (node) {
        node.removeAttribute("hidden");
      });
    }
  }

  function setSpin(target, on, tip) {
    if (!target) return;
    var overlay = target.querySelector(":scope > .ant-spin-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.className = "ant-spin-overlay";
      overlay.hidden = true;
      overlay.innerHTML =
        '<div class="ant-spin ant-spin-spinning" role="status">' +
          '<span class="ant-spin-dot ant-spin-dot-spin">' +
            '<i class="ant-spin-dot-item"></i><i class="ant-spin-dot-item"></i>' +
            '<i class="ant-spin-dot-item"></i><i class="ant-spin-dot-item"></i>' +
          "</span>" +
          '<span class="ant-spin-text"></span>' +
        "</div>";
      target.classList.add("ant-spin-nested");
      target.appendChild(overlay);
    }
    var text = overlay.querySelector(".ant-spin-text");
    if (tip && text) text.textContent = tip;
    overlay.hidden = !on;
    target.classList.toggle("is-spinning", !!on);
    target.setAttribute("aria-busy", on ? "true" : "false");
  }

  function filenameFromDisposition(header) {
    var encoded = /filename\*=UTF-8''([^;]+)/i.exec(header || "");
    if (encoded) {
      try { return decodeURIComponent(encoded[1].trim()); } catch (e) { return encoded[1].trim(); }
    }
    var plain = /filename="?([^";]+)"?/i.exec(header || "");
    return plain ? plain[1] : "export.csv";
  }

  function downloadExport(el, url, okText) {
    setButtonLoading(el, true);
    return fetch(url, { headers: { Accept: "text/csv, application/json" } })
      .then(function (res) {
        var ctype = (res.headers.get("content-type") || "").toLowerCase();
        if (!res.ok || ctype.indexOf("application/json") !== -1) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            throw new Error((data && data.error) || "导出失败");
          });
        }
        var filename = filenameFromDisposition(res.headers.get("content-disposition"));
        return res.blob().then(function (blob) {
          var objectUrl = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = objectUrl;
          a.download = filename;
          a.rel = "noopener";
          document.body.appendChild(a);
          a.click();
          a.remove();
          window.setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 1500);
          message("success", okText || "已开始下载");
        });
      })
      .catch(function (err) {
        message("error", (err && err.message) || "导出失败");
      })
      .then(function () {
        setButtonLoading(el, false);
      });
  }

  function upload(url, file, options) {
    var opts = options || {};
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.upload.onprogress = function (ev) {
        if (!opts.onProgress || !ev.lengthComputable || !ev.total) return;
        opts.onProgress(Math.round((ev.loaded / ev.total) * 100));
      };
      xhr.onerror = function () { reject(new Error("上传失败")); };
      xhr.onload = function () {
        var data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { data = {}; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.error || "导入失败"));
      };
      var body = new FormData();
      body.append(opts.field || "file", file);
      xhr.send(body);
    });
  }

  document.addEventListener("click", function (ev) {
    var el = ev.target && ev.target.closest ? ev.target.closest("a[data-antd-export]") : null;
    if (!el) return;
    ev.preventDefault();
    if (el.dataset.antdLoading === "1") return;
    downloadExport(el, el.href, el.dataset.successText || "已开始下载");
  });

  window.AntdFeedback = {
    message: message,
    notification: notification,
    setButtonLoading: setButtonLoading,
    setSpin: setSpin,
    downloadExport: downloadExport,
    upload: upload,
    escapeHtml: escapeHtml,
  };
})();
