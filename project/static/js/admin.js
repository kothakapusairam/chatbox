/* Administration console: overview, user management, payments and audit log. */
(function () {
  "use strict";

  function kv(label, value) {
    return App.el("div", {}, [App.el("dt", { text: label }), App.el("dd", { text: value == null ? "—" : String(value) })]);
  }

  function pager(prev, next, info, state, load) {
    prev.onclick = function () { if (state.page > 1) { state.page--; load(); } };
    next.onclick = function () { if (state.page < state.totalPages) { state.page++; load(); } };
    prev.disabled = state.page <= 1;
    next.disabled = state.page >= state.totalPages;
    info.textContent = "Page " + state.page + " of " + state.totalPages;
  }

  // ---------- tabs ----------
  var loaders = {};
  document.querySelectorAll(".tab[data-tab]").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".tab[data-tab]").forEach(function (t) {
        var on = t === tab;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      var name = tab.getAttribute("data-tab");
      document.querySelectorAll("[data-section]").forEach(function (s) { s.hidden = s.id !== "tab-" + name; });
      if (loaders[name]) loaders[name]();
    });
  });

  document.getElementById("agentLogoutBtn").addEventListener("click", async function () {
    await App.api("/api/agent/logout", { method: "POST" });
    window.location.assign("/agent/login");
  });

  // ---------- overview ----------
  async function loadOverview() {
    var r = await App.api("/api/admin/overview");
    if (!r.ok) { App.showToast(App.errorMessage(r, "Could not load overview."), "error"); return; }
    var d = r.data;
    document.getElementById("mCollected").textContent = App.formatMoney(d.payments.collected_cents, "usd");
    document.getElementById("mRefunded").textContent = App.formatMoney(d.payments.refunded_cents, "usd");
    document.getElementById("mOpen").textContent = d.tickets.open;
    document.getElementById("mFailedRefunds").textContent = d.payments.failed_refunds;
    App.clear(document.getElementById("paymentInfo")).append(
      kv("Provider", d.payments.provider),
      kv("Successful payments", d.payments.succeeded_count),
      kv("Last webhook received", App.formatDateTime(d.payments.last_webhook_at)),
      kv("Orders by payment state", Object.keys(d.orders_by_payment_status).map(function (k) {
        return k.replace(/_/g, " ") + ": " + d.orders_by_payment_status[k];
      }).join(" • ") || "—")
    );
    App.clear(document.getElementById("systemInfo")).append(
      kv("Version", d.system.version),
      kv("Environment", d.system.environment),
      kv("Intent model", d.system.model_loaded ? "Loaded" : "Not loaded"),
      kv("Model accuracy (held-out)", d.system.model_metadata && d.system.model_metadata.evaluation_metrics
        ? (d.system.model_metadata.evaluation_metrics.accuracy * 100).toFixed(1) + "%" : "—"),
      kv("Agent accounts", d.system.agents),
      kv("Audit entries", d.system.audit_events)
    );
  }
  loaders.overview = loadOverview;

  // ---------- users ----------
  var usersBody = document.getElementById("usersBody");
  var secretBox = document.getElementById("secretBox");

  function userRow(u) {
    var access = App.el("select", { className: "form-control compact", "aria-label": "Access for " + u.username }, [
      App.el("option", { value: "agent", text: "Support agent", selected: u.access_level !== "admin" }),
      App.el("option", { value: "admin", text: "Administrator", selected: u.access_level === "admin" }),
    ]);
    var active = App.el("input", { type: "checkbox", checked: !!u.is_active, "aria-label": "Active" });
    var saveBtn = App.el("button", { type: "button", className: "btn btn-outline btn-sm", text: "Save" });
    var resetBtn = App.el("button", { type: "button", className: "btn btn-secondary btn-sm", text: "Reset password" });
    var state = u.must_change_password ? "must change password" : "active";
    if (!u.is_active) state = "deactivated";
    if (u.locked_until) state = "locked";

    saveBtn.addEventListener("click", async function () {
      var body = { access_level: access.value, is_active: active.checked };
      var r = await App.api("/api/admin/users/" + encodeURIComponent(u.username), { method: "PATCH", body: body });
      if (r.ok) { App.showToast("Saved " + u.username + ".", "success"); loadUsers(); }
      else App.showToast(App.errorMessage(r, "Could not save."), "error");
    });
    resetBtn.addEventListener("click", async function () {
      if (!window.confirm("Generate a new temporary password for " + u.username + "?")) return;
      var r = await App.api("/api/admin/users/" + encodeURIComponent(u.username) + "/reset-password", { method: "POST" });
      if (r.ok) {
        App.clear(secretBox).append(
          App.el("strong", { text: "Temporary password for " + u.username + ": " }),
          App.el("code", { text: r.data.temporary_password }),
          App.el("div", { className: "tiny", text: "Share it securely. It will not be shown again." })
        );
        secretBox.hidden = false;
        loadUsers();
      } else App.showToast(App.errorMessage(r, "Could not reset password."), "error");
    });

    return App.el("tr", {}, [
      App.el("td", {}, [App.el("div", { className: "strong", text: u.full_name }),
                         App.el("div", { className: "tiny muted", text: "@" + u.username + (u.email ? " • " + u.email : "") })]),
      App.el("td", {}, [access]),
      App.el("td", {}, [App.el("label", { className: "inline-check" }, [active, App.el("span", { text: " " + state })])]),
      App.el("td", { className: "tiny muted", text: App.formatDateTime(u.last_login_at) }),
      App.el("td", {}, [App.el("div", { className: "btn-row" }, [saveBtn, resetBtn])]),
    ]);
  }

  async function loadUsers() {
    var r = await App.api("/api/admin/users");
    App.clear(usersBody);
    if (!r.ok) { usersBody.appendChild(App.el("tr", {}, [App.el("td", { colspan: "5", text: App.errorMessage(r, "Could not load users.") })])); return; }
    r.data.users.forEach(function (u) { usersBody.appendChild(userRow(u)); });
  }
  loaders.users = loadUsers;

  var createForm = document.getElementById("createUserForm");
  var cuError = document.getElementById("cuError");
  createForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    cuError.hidden = true;
    var body = {
      username: document.getElementById("cuUsername").value.trim().toLowerCase(),
      full_name: document.getElementById("cuFullName").value.trim(),
      email: document.getElementById("cuEmail").value.trim(),
      access_level: document.getElementById("cuAccess").value,
      password: document.getElementById("cuPassword").value,
    };
    var r = await App.api("/api/admin/users", { method: "POST", body: body });
    if (r.ok) {
      createForm.reset();
      App.showToast(r.data.message, "success");
      loadUsers();
    } else {
      cuError.textContent = App.errorMessage(r, "Could not create the user.");
      cuError.hidden = false;
    }
  });

  // ---------- payments ----------
  var paymentsBody = document.getElementById("paymentsBody");
  var paymentState = { page: 1, totalPages: 1 };
  async function loadPayments() {
    var status = document.getElementById("paymentStatusFilter").value;
    var params = new URLSearchParams({ page: String(paymentState.page), limit: "20" });
    if (status !== "All") params.set("status", status);
    var r = await App.api("/api/admin/payments?" + params.toString());
    App.clear(paymentsBody);
    if (!r.ok) { paymentsBody.appendChild(App.el("tr", {}, [App.el("td", { colspan: "7", text: App.errorMessage(r, "Could not load payments.") })])); return; }
    if (!r.data.payments.length) paymentsBody.appendChild(App.el("tr", {}, [App.el("td", { colspan: "7", text: "No payments." })]));
    r.data.payments.forEach(function (p) {
      paymentsBody.appendChild(App.el("tr", {}, [
        App.el("td", { text: String(p.id) }),
        App.el("td", {}, [App.el("code", { text: p.order_id })]),
        App.el("td", { text: App.formatMoney(p.amount_cents, p.currency) }),
        App.el("td", {}, [App.el("span", { className: "badge pay-" + App.slug(p.status), text: p.status })]),
        App.el("td", { text: p.provider }),
        App.el("td", { className: "tiny muted", text: App.formatDateTime(p.created_at) }),
        App.el("td", { className: "tiny muted", text: App.formatDateTime(p.paid_at) }),
      ]));
    });
    paymentState.totalPages = r.data.total_pages;
    pager(document.getElementById("paymentPrev"), document.getElementById("paymentNext"),
          document.getElementById("paymentsInfo"), paymentState, loadPayments);
  }
  loaders.payments = loadPayments;
  document.getElementById("paymentFilterBtn").addEventListener("click", function () { paymentState.page = 1; loadPayments(); });

  // ---------- audit ----------
  var auditBody = document.getElementById("auditBody");
  var auditState = { page: 1, totalPages: 1 };
  async function loadAudit() {
    var r = await App.api("/api/admin/audit?page=" + auditState.page + "&limit=50");
    App.clear(auditBody);
    if (!r.ok) { auditBody.appendChild(App.el("tr", {}, [App.el("td", { colspan: "6", text: App.errorMessage(r, "Could not load audit log.") })])); return; }
    r.data.entries.forEach(function (e) {
      var target = e.target_type ? e.target_type + " " + (e.target_id || "") : "—";
      auditBody.appendChild(App.el("tr", {}, [
        App.el("td", { className: "tiny muted", text: App.formatDateTime(e.created_at) }),
        App.el("td", { text: e.actor }),
        App.el("td", { text: e.action }),
        App.el("td", { text: target }),
        App.el("td", { className: "tiny", text: e.details ? JSON.stringify(e.details) : "" }),
        App.el("td", { className: "tiny muted", text: e.ip_address || "" }),
      ]));
    });
    auditState.totalPages = Math.max(1, Math.ceil(r.data.total / 50));
    pager(document.getElementById("auditPrev"), document.getElementById("auditNext"),
          document.getElementById("auditInfo"), auditState, loadAudit);
  }
  loaders.audit = loadAudit;

  loadOverview();
})();
