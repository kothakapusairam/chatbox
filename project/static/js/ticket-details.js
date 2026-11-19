/* Agent ticket detail page: transcript, internal notes, status/priority/assignment updates. */
(function () {
  "use strict";

  var main = document.getElementById("ticketPage");
  var ticketId = main.getAttribute("data-ticket-id");
  var notesBox = document.getElementById("notesContainer");
  var transcriptBox = document.getElementById("transcriptContainer");
  var statusSelect = document.getElementById("statusSelect");
  var prioritySelect = document.getElementById("prioritySelect");
  var assignInput = document.getElementById("assignAgentInput");
  var notesInput = document.getElementById("resolutionNotesTextarea");
  var saveBtn = document.getElementById("saveTicketBtn");
  var statusBadge = document.getElementById("headerStatusBadge");
  var priorityBadge = document.getElementById("headerPriorityBadge");
  var updatedAt = document.getElementById("ticketUpdatedTimestamp");

  function setBadge(node, prefix, text) {
    node.className = "badge " + prefix + App.slug(text);
    node.textContent = text;
  }

  function renderNotes(notes) {
    App.clear(notesBox);
    if (!notes || !notes.length) {
      notesBox.appendChild(App.el("p", { className: "muted small", text: "No internal notes yet." }));
      return;
    }
    notes.forEach(function (n) {
      notesBox.appendChild(App.el("div", { className: "note-item" }, [
        App.el("div", { className: "note-header" }, [
          App.el("span", { className: "note-author", text: "👤 " + n.agent_username }),
          App.el("span", { text: App.formatDateTime(n.created_at) }),
        ]),
        App.el("div", { className: "note-body", text: n.note }),
      ]));
    });
  }

  function renderTranscript(messages) {
    App.clear(transcriptBox);
    if (!messages || !messages.length) {
      transcriptBox.appendChild(App.el("p", { className: "muted small",
        text: "No chatbot conversation is linked to this ticket." }));
      return;
    }
    messages.forEach(function (m) {
      var who = m.sender === "user" ? "Customer" : "Chatbot";
      var line = App.el("div", { className: "transcript-line " + m.sender }, [
        App.el("strong", { text: who + ": " }),
      ]);
      App.appendRichText(line, m.text);
      line.appendChild(App.el("span", { className: "tiny muted",
        text: "  " + new Date(m.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) }));
      transcriptBox.appendChild(line);
    });
    transcriptBox.scrollTop = transcriptBox.scrollHeight;
  }

  async function load() {
    var result = await App.api("/api/agent/tickets/" + encodeURIComponent(ticketId));
    if (!result.ok) {
      App.showToast(App.errorMessage(result, "Could not load ticket."), "error");
      return;
    }
    var t = result.data.ticket;
    notesInput.value = t.resolution_notes || "";
    assignInput.value = t.assigned_agent || "";
    statusSelect.value = t.status;
    prioritySelect.value = t.priority;
    document.getElementById("issueText").textContent = t.issue_description;
    renderNotes(result.data.notes);
    renderTranscript(result.data.messages);
  }

  document.getElementById("ticketUpdateForm").addEventListener("submit", async function (event) {
    event.preventDefault();
    saveBtn.disabled = true;
    var result = await App.api("/api/agent/tickets/" + encodeURIComponent(ticketId), {
      method: "PATCH",
      body: {
        status: statusSelect.value,
        priority: prioritySelect.value,
        assigned_agent: assignInput.value.trim() || null,
        resolution_notes: notesInput.value.trim(),
      },
    });
    saveBtn.disabled = false;
    if (result.ok) {
      App.showToast("Ticket updated.", "success");
      setBadge(statusBadge, "badge-", result.data.ticket.status);
      setBadge(priorityBadge, "priority-", result.data.ticket.priority);
      updatedAt.textContent = result.data.ticket.updated_at;
      load();
    } else {
      App.showToast(App.errorMessage(result, "Could not save changes."), "error");
    }
  });

  document.getElementById("addNoteForm").addEventListener("submit", async function (event) {
    event.preventDefault();
    var input = document.getElementById("newNoteInput");
    var note = input.value.trim();
    if (!note) return;
    var result = await App.api("/api/agent/tickets/" + encodeURIComponent(ticketId) + "/notes", {
      method: "POST", body: { note: note },
    });
    if (result.ok) {
      input.value = "";
      App.showToast("Note added.", "success");
      load();
    } else {
      App.showToast(App.errorMessage(result, "Could not add note."), "error");
    }
  });

  load();
})();
