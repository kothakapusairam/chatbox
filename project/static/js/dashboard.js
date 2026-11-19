/* Agent dashboard: ticket queue, filters, pagination and tab switching. */
(function () {
  "use strict";

  var PAGE_SIZE = 15;
  var state = { page: 1, totalPages: 1 };
  var el = {
    body: document.getElementById("ticketsTableBody"),
    info: document.getElementById("paginationInfo"),
    prev: document.getElementById("prevPageBtn"),
    next: document.getElementById("nextPageBtn"),
    search: document.getElementById("searchInput"),
    status: document.getElementById("statusFilter"),
    priority: document.getElementById("priorityFilter"),
    stats: {
      total: document.getElementById("statTotal"),
      open: document.getElementById("statOpen"),
      inProgress: document.getElementById("statInProgress"),
      resolved: document.getElementById("statResolved"),
      urgent: document.getElementById("statUrgent"),
    },
  };

  function messageRow(text, isError) {
    var tr = App.el("tr");
    tr.appendChild(App.el("td", {
      colspan: "9", className: "empty-cell" + (isError ? " error-text" : ""), text: text,
    }));
    return tr;
  }

  function buildRow(ticket) {
    var issue = ticket.issue_description || "";
    var preview = issue.length > 60 ? issue.slice(0, 57) + "…" : issue;
    var assigned = ticket.assigned_agent
      ? App.el("span", { text: "👤 " + ticket.assigned_agent })
      : App.el("span", { className: "muted", text: "Unassigned" });

    return App.el("tr", {}, [
      App.el("td", {}, [App.el("code", { text: ticket.ticket_id })]),
      App.el("td", {}, [
        App.el("div", { className: "strong", text: ticket.customer_name || "Customer" }),
        App.el("div", { className: "tiny muted", text: ticket.customer_email || "No email" }),
      ]),
      App.el("td", { title: issue, text: preview }),
      App.el("td", {}, [App.el("span", { className: "intent-pill",
                                         text: (ticket.predicted_category || "").replace(/_/g, " ") })]),
      App.el("td", {}, [App.el("span", { className: "badge priority-" + App.slug(ticket.priority),
                                         text: ticket.priority })]),
      App.el("td", {}, [App.el("span", { className: "badge badge-" + App.slug(ticket.status),
                                         text: ticket.status })]),
      App.el("td", {}, [assigned]),
      App.el("td", { className: "tiny muted", text: App.formatDateTime(ticket.created_at) }),
      App.el("td", {}, [App.el("a", {
        href: "/agent/tickets/" + encodeURIComponent(ticket.ticket_id),
        className: "btn btn-outline btn-sm", text: "Open →",
      })]),
    ]);
  }

  function updateStats(stats) {
    if (!stats) return;
    el.stats.total.textContent = stats.total || 0;
    el.stats.open.textContent = stats.open || 0;
    el.stats.inProgress.textContent = stats.in_progress || 0;
    el.stats.resolved.textContent = stats.resolved || 0;
    el.stats.urgent.textContent = stats.urgent_high || 0;
  }

  async function loadTickets() {
    App.clear(el.body).appendChild(messageRow("Loading tickets…"));
    var params = new URLSearchParams({ page: String(state.page), limit: String(PAGE_SIZE) });
    if (el.status.value !== "All") params.set("status", el.status.value);
    if (el.priority.value !== "All") params.set("priority", el.priority.value);
    if (el.search.value.trim()) params.set("search", el.search.value.trim());

    var result = await App.api("/api/agent/tickets?" + params.toString());
    if (result.status === 401) { window.location.assign("/agent/login"); return; }
    if (!result.ok) {
      App.clear(el.body).appendChild(messageRow(App.errorMessage(result, "Could not load tickets."), true));
      return;
    }
    updateStats(result.data.stats);
    App.clear(el.body);
    if (!result.data.tickets.length) {
      el.body.appendChild(messageRow("No support tickets match the current filters."));
    } else {
      result.data.tickets.forEach(function (t) { el.body.appendChild(buildRow(t)); });
    }
    state.totalPages = result.data.total_pages || 1;
    el.info.textContent = "Showing " + result.data.tickets.length + " of " + result.data.total +
      " tickets • Page " + state.page + " of " + state.totalPages;
    el.prev.disabled = state.page <= 1;
    el.next.disabled = state.page >= state.totalPages;
  }

  function resetAndLoad() { state.page = 1; loadTickets(); }

  document.getElementById("applyFiltersBtn").addEventListener("click", resetAndLoad);
  el.search.addEventListener("keydown", function (e) { if (e.key === "Enter") resetAndLoad(); });
  document.getElementById("resetFiltersBtn").addEventListener("click", function () {
    el.search.value = "";
    el.status.value = "All";
    el.priority.value = "All";
    resetAndLoad();
  });
  document.getElementById("refreshTicketsBtn").addEventListener("click", function () {
    loadTickets();
    App.showToast("Tickets refreshed.", "info");
  });
  el.prev.addEventListener("click", function () { if (state.page > 1) { state.page--; loadTickets(); } });
  el.next.addEventListener("click", function () { if (state.page < state.totalPages) { state.page++; loadTickets(); } });

  document.getElementById("agentLogoutBtn").addEventListener("click", async function () {
    await App.api("/api/agent/logout", { method: "POST" });
    window.location.assign("/agent/login");
  });

  // Tabs
  document.querySelectorAll(".tab[data-tab]").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".tab[data-tab]").forEach(function (t) {
        var active = t === tab;
        t.classList.toggle("active", active);
        t.setAttribute("aria-selected", active ? "true" : "false");
      });
      document.querySelectorAll("[data-section]").forEach(function (section) {
        section.hidden = section.id !== "tab-" + tab.getAttribute("data-tab");
      });
    });
  });

  loadTickets();
})();
