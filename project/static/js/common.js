/*
 * Shared front-end helpers. Loaded on every page before page-specific scripts.
 * Everything rendered from server data goes through textContent / DOM APIs,
 * never innerHTML, so stored customer text cannot execute as markup.
 */
(function (global) {
  "use strict";

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function setCsrfToken(token) {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && token) meta.setAttribute("content", token);
  }

  /** fetch wrapper: JSON in/out, CSRF header on writes, uniform error shape. */
  async function api(url, options) {
    const opts = options || {};
    const method = (opts.method || "GET").toUpperCase();
    const headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
    let body;
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
    if (method !== "GET" && method !== "HEAD") {
      headers["X-CSRF-Token"] = csrfToken();
    }
    let response;
    try {
      response = await fetch(url, { method: method, headers: headers, body: body, credentials: "same-origin" });
    } catch (err) {
      return { ok: false, status: 0, data: { message: "Network error. Check your connection and try again." } };
    }
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      data = {};
    }
    if (data && data.csrf_token) setCsrfToken(data.csrf_token);
    return {
      ok: response.ok,
      status: response.status,
      data: data,
      retryAfter: response.headers.get("Retry-After"),
    };
  }

  /** Build an element safely. attrs: className, text, dataset, on{Event} handlers, anything else as attribute. */
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    const a = attrs || {};
    Object.keys(a).forEach(function (key) {
      const value = a[key];
      if (value === undefined || value === null || value === false) return;
      if (key === "className") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "dataset") Object.keys(value).forEach(function (d) { node.dataset[d] = value[d]; });
      else if (key.indexOf("on") === 0) node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, "");
      else node.setAttribute(key, String(value));
    });
    (children || []).forEach(function (child) {
      if (child === undefined || child === null) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  /**
   * Minimal rich text: **bold**, `code`, http(s) links and line breaks.
   * Output is DOM nodes only; untrusted text is never parsed as HTML.
   */
  const TOKEN = /(\*\*[^*\n]+\*\*|`[^`\n]+`|https?:\/\/[^\s<>"'`]+)/g;
  function appendRichText(container, text) {
    String(text == null ? "" : text).split("\n").forEach(function (line, index) {
      if (index > 0) container.appendChild(document.createElement("br"));
      let last = 0;
      let match;
      TOKEN.lastIndex = 0;
      while ((match = TOKEN.exec(line)) !== null) {
        if (match.index > last) container.appendChild(document.createTextNode(line.slice(last, match.index)));
        const token = match[0];
        if (token.indexOf("**") === 0) {
          container.appendChild(el("strong", { text: token.slice(2, -2) }));
        } else if (token.charAt(0) === "`") {
          container.appendChild(el("code", { className: "inline-code", text: token.slice(1, -1) }));
        } else {
          container.appendChild(el("a", { href: token, target: "_blank", rel: "noopener noreferrer", text: token }));
        }
        last = match.index + token.length;
      }
      if (last < line.length) container.appendChild(document.createTextNode(line.slice(last)));
    });
    return container;
  }

  function showToast(message, type) {
    const container = document.getElementById("toastContainer");
    if (!container) return;
    const toast = el("div", { className: "toast toast-" + (type || "info"), role: "status", text: message });
    container.appendChild(toast);
    setTimeout(function () {
      toast.style.opacity = "0";
      setTimeout(function () { toast.remove(); }, 300);
    }, 4000);
  }

  function formatDateTime(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (isNaN(date.getTime())) return String(iso);
    return date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }

  function formatMoney(cents, currency) {
    const code = (currency || "usd").toLowerCase();
    const symbols = { usd: "$", eur: "€", gbp: "£", inr: "₹" };
    const amount = (Number(cents || 0) / 100).toLocaleString([], { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (symbols[code] || "") + (symbols[code] ? amount : amount + " " + code.toUpperCase());
  }

  function slug(text) {
    return String(text || "").toLowerCase().replace(/\s+/g, "-");
  }

  function errorMessage(result, fallback) {
    return (result && result.data && result.data.message) || fallback || "Something went wrong.";
  }

  global.App = {
    api: api,
    el: el,
    clear: clear,
    appendRichText: appendRichText,
    showToast: showToast,
    formatDateTime: formatDateTime,
    formatMoney: formatMoney,
    slug: slug,
    errorMessage: errorMessage,
    setCsrfToken: setCsrfToken,
  };
})(window);
