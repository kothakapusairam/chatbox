/* Public ticket tracker. Requires ticket ID + the email used on the ticket. */
(function () {
  "use strict";

  var form = document.getElementById("trackForm");
  var idInput = document.getElementById("ticketIdInput");
  var emailInput = document.getElementById("ticketEmailInput");
  var button = document.getElementById("trackBtn");
  var result = document.getElementById("result");

  function show(nodes, kind) {
    App.clear(result);
    result.className = kind === "error" ? "alert alert-error" : "ticket-result";
    nodes.forEach(function (n) { result.appendChild(n); });
    result.hidden = false;
  }

  function row(label, value) {
    return App.el("div", { className: "info-item" }, [
      App.el("div", { className: "info-label", text: label }),
      App.el("div", { className: "info-value", text: value }),
    ]);
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var rawId = idInput.value.trim().toUpperCase();
    if (rawId && rawId.indexOf("TKT-") !== 0) rawId = "TKT-" + rawId;
    var email = emailInput.value.trim().toLowerCase();
    if (!rawId || !email) {
      show([document.createTextNode("Enter both the ticket ID and the email address used on the ticket.")], "error");
      return;
    }
    button.disabled = true;
    var response = await App.api("/api/tickets/" + encodeURIComponent(rawId) + "?email=" + encodeURIComponent(email));
    button.disabled = false;
    if (!response.ok) {
      show([document.createTextNode(App.errorMessage(response, "No ticket matches those details."))], "error");
      return;
    }
    var t = response.data;
    show([
      App.el("div", { className: "ticket-result-header" }, [
        App.el("div", { className: "ticket-id", text: "🎫 " + t.ticket_id }),
        App.el("div", { className: "ticket-subject", text: t.category.replace(/_/g, " ") }),
      ]),
      App.el("div", { className: "ticket-result-body" }, [
        App.el("div", { className: "info-grid" }, [
          row("Status", t.status),
          row("Priority", t.priority),
          row("Opened", App.formatDateTime(t.created_at)),
          row("Last update", App.formatDateTime(t.updated_at)),
        ]),
        App.el("div", { className: "callout top-gap" }, [
          App.el("div", { className: "info-label", text: "Your issue" }),
          App.el("p", { className: "pre-wrap", text: t.description }),
        ]),
      ]),
    ], "ok");
  });

  var params = new URLSearchParams(window.location.search);
  if (params.get("id")) idInput.value = params.get("id");
})();
