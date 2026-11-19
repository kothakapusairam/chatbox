/*
 * Agent orders & refunds panel.
 * Refunds send an Idempotency-Key per submission so a double click cannot refund twice.
 */
(function () {
  "use strict";

  var PAGE_SIZE = 15;
  var state = { page: 1, totalPages: 1, selected: null };
  var el = {
    body: document.getElementById("ordersTableBody"),
    info: document.getElementById("orderPaginationInfo"),
    prev: document.getElementById("orderPrevBtn"),
    next: document.getElementById("orderNextBtn"),
    search: document.getElementById("orderSearch"),
    payment: document.getElementById("orderPaymentFilter"),
    detail: document.getElementById("orderDetail"),
  };

  function cell(content) {
    return App.el("td", {}, Array.isArray(content) ? content : [content]);
  }

  async function loadOrders() {
    App.clear(el.body).appendChild(App.el("tr", {}, [App.el("td", {
      colspan: "5", className: "empty-cell", text: "Loading orders…" })]));
    var params = new URLSearchParams({ page: String(state.page), limit: String(PAGE_SIZE) });
    if (el.search.value.trim()) params.set("search", el.search.value.trim());
    if (el.payment.value !== "All") params.set("payment_status", el.payment.value);
    var result = await App.api("/api/agent/orders?" + params.toString());
    App.clear(el.body);
    if (!result.ok) {
      el.body.appendChild(App.el("tr", {}, [App.el("td", { colspan: "5", className: "empty-cell error-text",
        text: App.errorMessage(result, "Could not load orders.") })]));
      return;
    }
    if (!result.data.orders.length) {
      el.body.appendChild(App.el("tr", {}, [App.el("td", { colspan: "5", className: "empty-cell",
        text: "No orders match." })]));
    }
    result.data.orders.forEach(function (o) {
      el.body.appendChild(App.el("tr", {
        className: "clickable", tabindex: "0",
        onclick: function () { openOrder(o.order_id); },
        onkeydown: function (e) { if (e.key === "Enter") openOrder(o.order_id); },
      }, [
        cell(App.el("code", { text: o.order_id })),
        cell([App.el("div", { className: "strong", text: o.customer_name }),
              App.el("div", { className: "tiny muted", text: o.customer_email || "No email" })]),
        cell(App.formatMoney(o.amount_cents, o.currency)),
        cell(App.el("span", { className: "badge badge-" + App.slug(o.status), text: o.status })),
        cell(App.el("span", { className: "badge pay-" + App.slug(o.payment_status), text: o.payment_status.replace(/_/g, " ") })),
      ]));
    });
    state.totalPages = result.data.total_pages || 1;
    el.info.textContent = "Page " + state.page + " of " + state.totalPages + " • " + result.data.total + " orders";
    el.prev.disabled = state.page <= 1;
    el.next.disabled = state.page >= state.totalPages;
  }

  function kv(label, value) {
    return App.el("div", {}, [App.el("dt", { text: label }), App.el("dd", { text: value == null ? "—" : String(value) })]);
  }

  async function openOrder(orderId) {
    state.selected = orderId;
    App.clear(el.detail).appendChild(App.el("p", { className: "muted", text: "Loading " + orderId + "…" }));
    var result = await App.api("/api/agent/orders/" + encodeURIComponent(orderId));
    if (!result.ok) {
      App.clear(el.detail).appendChild(App.el("p", { className: "error-text",
        text: App.errorMessage(result, "Could not load order.") }));
      return;
    }
    renderDetail(result.data);
  }

  function renderDetail(data) {
    var o = data.order;
    var container = App.clear(el.detail);
    container.appendChild(App.el("h2", { className: "section-title", text: "Order " + o.order_id }));
    container.appendChild(App.el("dl", { className: "kv-grid" }, [
      kv("Customer", o.customer_name),
      kv("Email", o.customer_email),
      kv("Order status", o.status),
      kv("Payment", o.payment_status.replace(/_/g, " ")),
      kv("Total", App.formatMoney(o.amount_cents, o.currency)),
      kv("Carrier", o.carrier),
      kv("Items", o.items),
    ]));

    container.appendChild(App.el("h3", { className: "subsection-title", text: "Payments" }));
    if (!data.payments.length) {
      container.appendChild(App.el("p", { className: "muted small", text: "No payment attempts yet." }));
    } else {
      container.appendChild(App.el("ul", { className: "plain-list" }, data.payments.map(function (p) {
        return App.el("li", {}, [App.el("span", { text: App.formatMoney(p.amount_cents, p.currency) + " • " +
          p.status + " • " + p.provider + " • " + App.formatDateTime(p.created_at) })]);
      })));
    }

    container.appendChild(App.el("h3", { className: "subsection-title", text: "Refunds" }));
    if (!data.refunds.length) {
      container.appendChild(App.el("p", { className: "muted small", text: "No refunds." }));
    } else {
      container.appendChild(App.el("ul", { className: "plain-list" }, data.refunds.map(function (r) {
        return App.el("li", {}, [App.el("span", { text: App.formatMoney(r.amount_cents, r.currency) + " • " +
          r.status + " • by " + r.initiated_by + (r.reason ? " • " + r.reason : "") })]);
      })));
    }

    if (data.tickets.length) {
      container.appendChild(App.el("h3", { className: "subsection-title", text: "Linked tickets" }));
      container.appendChild(App.el("ul", { className: "plain-list" }, data.tickets.map(function (t) {
        return App.el("li", {}, [App.el("a", { href: "/agent/tickets/" + encodeURIComponent(t.ticket_id),
          text: t.ticket_id + " • " + t.status + " • " + t.priority })]);
      })));
    }

    if (data.payments.some(function (p) { return p.status === "succeeded"; }) && o.payment_status !== "refunded") {
      container.appendChild(buildRefundForm(o));
    }
    if (o.status === "Processing") {
      container.appendChild(App.el("button", {
        type: "button", className: "btn btn-danger btn-sm top-gap",
        text: "Cancel order" + (o.payment_status === "paid" ? " & refund" : ""),
        onclick: function () { cancelOrder(o.order_id); },
      }));
    }
  }

  function buildRefundForm(order) {
    var status = App.el("div", { className: "form-status", role: "status" });
    var amountInput = App.el("input", { type: "number", min: "0.01", step: "0.01", id: "refundAmount",
                                        className: "form-control", placeholder: "Full remaining amount" });
    var reasonInput = App.el("input", { type: "text", id: "refundReason", className: "form-control",
                                        maxlength: "300", placeholder: "Reason (required)", required: true });
    var button = App.el("button", { type: "submit", className: "btn btn-primary btn-sm", text: "Issue refund" });
    var form = App.el("form", { className: "refund-form top-gap", novalidate: true }, [
      App.el("h3", { className: "subsection-title", text: "Issue refund" }),
      App.el("div", { className: "form-group" }, [App.el("label", { for: "refundAmount", text: "Amount (" +
        order.currency.toUpperCase() + ")" }), amountInput]),
      App.el("div", { className: "form-group" }, [App.el("label", { for: "refundReason", text: "Reason" }), reasonInput]),
      status,
      button,
    ]);
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      status.textContent = "";
      var reason = reasonInput.value.trim();
      if (!reason) { status.textContent = "Please give a reason for the refund."; return; }
      var body = { reason: reason };
      if (amountInput.value.trim()) {
        var cents = Math.round(parseFloat(amountInput.value) * 100);
        if (!(cents > 0)) { status.textContent = "Enter a valid amount."; return; }
        body.amount_cents = cents;
      }
      button.disabled = true;
      var key = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random();
      var result = await App.api("/api/agent/orders/" + encodeURIComponent(order.order_id) + "/refunds", {
        method: "POST", body: body, headers: { "Idempotency-Key": key },
      });
      button.disabled = false;
      if (result.ok) {
        App.showToast("Refund submitted.", "success");
        openOrder(order.order_id);
        loadOrders();
      } else {
        status.textContent = App.errorMessage(result, "Refund failed.");
      }
    });
    return form;
  }

  async function cancelOrder(orderId) {
    if (!window.confirm("Cancel order " + orderId + "? Any payment will be refunded in full.")) return;
    var result = await App.api("/api/agent/orders/" + encodeURIComponent(orderId) + "/cancel", { method: "POST" });
    if (result.ok) {
      App.showToast("Order cancelled. Outcome: " + result.data.result.outcome.replace(/_/g, " "), "success");
      openOrder(orderId);
      loadOrders();
    } else {
      App.showToast(App.errorMessage(result, "Could not cancel order."), "error");
    }
  }

  document.getElementById("orderSearchBtn").addEventListener("click", function () { state.page = 1; loadOrders(); });
  el.search.addEventListener("keydown", function (e) { if (e.key === "Enter") { state.page = 1; loadOrders(); } });
  el.payment.addEventListener("change", function () { state.page = 1; loadOrders(); });
  el.prev.addEventListener("click", function () { if (state.page > 1) { state.page--; loadOrders(); } });
  el.next.addEventListener("click", function () { if (state.page < state.totalPages) { state.page++; loadOrders(); } });

  // Deep link from a ticket: /agent/dashboard?order=ORD1025 opens the Orders tab.
  var params = new URLSearchParams(window.location.search);
  if (params.get("order")) {
    var tab = document.querySelector('.tab[data-tab="orders"]');
    if (tab) tab.click();
    el.search.value = params.get("order");
    state.page = 1;
    openOrder(params.get("order"));
  }
  loadOrders();
})();
