/*
 * Customer chat widget.
 * - Conversation id is kept in localStorage so the transcript survives reloads.
 * - All server text is rendered via App.appendRichText (no innerHTML).
 */
(function () {
  "use strict";

  var MAX = 500;
  var STORAGE_KEY = "cs_conversation_id";
  var chatHistory = document.getElementById("chatHistory");
  var chatForm = document.getElementById("chatForm");
  var userInput = document.getElementById("userInput");
  var sendBtn = document.getElementById("sendBtn");
  var charCount = document.getElementById("charCount");
  var newChatBtn = document.getElementById("newChatBtn");
  var conversationId = null;
  var busy = false;

  // ---------------- rendering ----------------

  function bubble(sender, text, intent, messageId, withFeedback) {
    var row = App.el("div", { className: "message-row " + sender });
    row.appendChild(App.el("div", { className: "avatar avatar-" + sender, "aria-hidden": "true",
                                    text: sender === "bot" ? "🤖" : "👤" }));
    var wrapper = App.el("div", { className: "bubble-wrapper" });
    var body = App.el("div", { className: "message-bubble" });
    App.appendRichText(body, text);
    if (sender === "bot" && withFeedback && messageId) {
      body.appendChild(feedbackRow(messageId));
    }
    wrapper.appendChild(body);
    var meta = App.el("div", { className: "message-meta" }, [
      App.el("span", { text: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) }),
    ]);
    if (sender === "bot" && intent && intent !== "greeting") {
      meta.appendChild(App.el("span", { className: "intent-pill", text: String(intent).replace(/_/g, " ") }));
    }
    wrapper.appendChild(meta);
    row.appendChild(wrapper);
    chatHistory.appendChild(row);
    return row;
  }

  function feedbackRow(messageId) {
    var wrap = App.el("div", { className: "bubble-feedback" });
    wrap.appendChild(App.el("span", { className: "feedback-label", text: "Helpful?" }));
    [["helpful", "👍 Yes"], ["unhelpful", "👎 No"]].forEach(function (pair) {
      wrap.appendChild(App.el("button", {
        type: "button", className: "feedback-btn", text: pair[1],
        onclick: function () { sendFeedback(wrap, messageId, pair[0]); },
      }));
    });
    return wrap;
  }

  function scrollToBottom() {
    chatHistory.scrollTop = chatHistory.scrollHeight;
  }

  function showWelcome() {
    bubble("bot", [
      "**Hello! Welcome to Customer Support.**",
      "I'm your virtual assistant. I can help with:",
      "• 📦 Order status and tracking (e.g. ORD1025)",
      "• 💳 Refunds and payments",
      "• 🔄 Returns and policies",
      "• 🔐 Account access and password resets",
      "• 👤 Connecting you with a human agent",
      "For order details I'll ask you to confirm the email address used at checkout.",
      "How can I help you today?",
    ].join("\n"), "greeting", null, false);
    scrollToBottom();
  }

  function renderHistory(messages) {
    App.clear(chatHistory);
    if (!messages || messages.length === 0) {
      showWelcome();
      return;
    }
    messages.forEach(function (m) {
      bubble(m.sender, m.text, m.predicted_intent, m.id, true);
    });
    scrollToBottom();
  }

  function setBusy(state) {
    busy = state;
    sendBtn.disabled = state;
    userInput.disabled = state;
  }

  function showTyping() {
    var row = App.el("div", { className: "message-row bot", id: "typingRow" }, [
      App.el("div", { className: "avatar avatar-bot", text: "🤖", "aria-hidden": "true" }),
      App.el("div", { className: "typing-indicator" }, [
        App.el("div", { className: "typing-dot" }), App.el("div", { className: "typing-dot" }),
        App.el("div", { className: "typing-dot" }),
      ]),
    ]);
    chatHistory.appendChild(row);
    scrollToBottom();
  }

  function hideTyping() {
    var row = document.getElementById("typingRow");
    if (row) row.remove();
  }

  // ---------------- session ----------------

  async function startConversation(announce) {
    var result = await App.api("/api/conversations", { method: "POST", body: { customer_name: "Customer" } });
    if (!result.ok) {
      App.showToast(App.errorMessage(result, "Could not start a chat session."), "error");
      return;
    }
    conversationId = result.data.conversation_id;
    localStorage.setItem(STORAGE_KEY, conversationId);
    App.clear(chatHistory);
    showWelcome();
    if (announce) App.showToast("Started a new conversation.", "success");
  }

  async function initSession() {
    conversationId = localStorage.getItem(STORAGE_KEY);
    if (!conversationId) {
      await startConversation(false);
      return;
    }
    var result = await App.api("/api/conversations/" + encodeURIComponent(conversationId));
    if (result.ok) {
      renderHistory(result.data.messages);
    } else {
      await startConversation(false);
    }
  }

  // ---------------- messaging ----------------

  async function sendMessage(presetText) {
    var text = (presetText || userInput.value || "").trim();
    if (!text || busy) return;
    if (text.length > MAX) {
      App.showToast("Messages can be at most " + MAX + " characters.", "error");
      return;
    }
    if (!presetText) {
      userInput.value = "";
      userInput.style.height = "auto";
      updateCounter();
    }
    bubble("user", text, null, null, false);
    scrollToBottom();
    setBusy(true);
    showTyping();

    var result = await App.api("/api/chat", {
      method: "POST",
      body: { conversation_id: conversationId, message: text },
    });
    hideTyping();
    setBusy(false);

    if (!result.ok) {
      var message = App.errorMessage(result, "We couldn't reach support. Please try again.");
      if (result.status === 429) message = "You're sending messages too quickly. Please wait a moment.";
      bubble("bot", "⚠️ " + message, "error", null, false);
      App.showToast(message, "error");
    } else {
      bubble("bot", result.data.response, result.data.intent, result.data.message_id, true);
      if (result.data.escalated && result.data.ticket_id) {
        App.showToast("Support ticket " + result.data.ticket_id + " opened.", "success");
      }
    }
    scrollToBottom();
    userInput.focus();
  }

  async function sendFeedback(wrap, messageId, rating) {
    var result = await App.api("/api/feedback", {
      method: "POST",
      body: { conversation_id: conversationId, message_id: messageId, rating: rating },
    });
    if (result.ok) {
      App.clear(wrap);
      wrap.appendChild(App.el("span", { className: "feedback-thanks", text: "✓ Thanks for your feedback!" }));
    } else {
      App.showToast(App.errorMessage(result, "Could not save feedback."), "error");
    }
  }

  function updateCounter() {
    var len = userInput.value.length;
    charCount.textContent = len + " / " + MAX;
    charCount.className = "char-counter" + (len >= 480 ? " limit-exceeded" : len >= 400 ? " limit-near" : "");
    userInput.style.height = "auto";
    userInput.style.height = Math.min(userInput.scrollHeight, 120) + "px";
  }

  chatForm.addEventListener("submit", function (event) {
    event.preventDefault();
    sendMessage();
  });
  userInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
  userInput.addEventListener("input", updateCounter);
  document.querySelectorAll(".quick-chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var query = chip.getAttribute("data-query");
      if (query) sendMessage(query);
    });
  });
  newChatBtn.addEventListener("click", function () {
    if (window.confirm("Start a new chat? This window's messages will be cleared.")) {
      startConversation(true);
    }
  });

  // ---------------- modals ----------------

  function openModal(id) {
    var modal = document.getElementById(id);
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    var first = modal.querySelector("input, textarea");
    if (first) first.focus();
  }

  function closeModal(id) {
    var modal = document.getElementById(id);
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }

  document.querySelectorAll(".modal-backdrop").forEach(function (backdrop) {
    backdrop.addEventListener("click", function (event) {
      if (event.target === backdrop) closeModal(backdrop.id);
    });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeModal("trackTicketModal");
      closeModal("escalationModal");
    }
  });

  // Track ticket
  var lookupResult = document.getElementById("ticketLookupResult");
  document.getElementById("trackTicketModalBtn").addEventListener("click", function () {
    lookupResult.hidden = true;
    App.clear(lookupResult);
    openModal("trackTicketModal");
  });
  document.getElementById("closeTrackModal").addEventListener("click", function () { closeModal("trackTicketModal"); });

  function renderLookupResult(nodes, isError) {
    App.clear(lookupResult);
    lookupResult.className = isError ? "alert alert-error" : "ticket-summary";
    nodes.forEach(function (node) { lookupResult.appendChild(node); });
    lookupResult.hidden = false;
  }

  document.getElementById("searchTicketBtn").addEventListener("click", async function () {
    var ticketId = document.getElementById("ticketLookupInput").value.trim().toUpperCase();
    var email = document.getElementById("ticketLookupEmail").value.trim().toLowerCase();
    if (!ticketId || !email) {
      renderLookupResult([document.createTextNode("Enter the ticket ID and the email used on the ticket.")], true);
      return;
    }
    var button = document.getElementById("searchTicketBtn");
    button.disabled = true;
    var result = await App.api("/api/tickets/" + encodeURIComponent(ticketId) + "?email=" + encodeURIComponent(email));
    button.disabled = false;
    if (!result.ok) {
      renderLookupResult([document.createTextNode(App.errorMessage(result, "No ticket matches those details."))], true);
      return;
    }
    var t = result.data;
    renderLookupResult([
      App.el("strong", { text: "🎫 " + t.ticket_id }),
      App.el("div", { className: "small", text: "Status: " + t.status + " • Priority: " + t.priority }),
      App.el("div", { className: "small muted", text: "Opened " + App.formatDateTime(t.created_at) }),
    ], false);
  });

  // Human support
  var escalateError = document.getElementById("escalateError");
  document.getElementById("escalateHeaderBtn").addEventListener("click", function () {
    escalateError.hidden = true;
    openModal("escalationModal");
  });
  document.getElementById("closeEscalationModal").addEventListener("click", function () { closeModal("escalationModal"); });

  document.getElementById("submitEscalationBtn").addEventListener("click", async function () {
    var issue = document.getElementById("escalateIssue").value.trim();
    var email = document.getElementById("escalateEmail").value.trim();
    escalateError.hidden = true;
    if (issue.length < 5) {
      escalateError.textContent = "Please describe your issue (at least 5 characters).";
      escalateError.hidden = false;
      return;
    }
    var button = document.getElementById("submitEscalationBtn");
    button.disabled = true;
    var result = await App.api("/api/tickets", {
      method: "POST",
      body: {
        conversation_id: conversationId,
        customer_name: document.getElementById("escalateName").value.trim() || "Customer",
        customer_email: email,
        issue_description: issue,
        predicted_category: "human_support",
      },
    });
    button.disabled = false;
    if (!result.ok) {
      escalateError.textContent = App.errorMessage(result, "Could not submit your request.");
      escalateError.hidden = false;
      return;
    }
    closeModal("escalationModal");
    document.getElementById("escalateIssue").value = "";
    bubble("bot", "👤 **Human support ticket created**\n\n• Ticket ID: `" + result.data.ticket_id + "`\n• Status: " +
           result.data.status + "\n• Priority: " + result.data.priority +
           "\n\nA support specialist will review your request and follow up.", "human_support", null, false);
    scrollToBottom();
    App.showToast("Ticket " + result.data.ticket_id + " created.", "success");
  });

  updateCounter();
  initSession();
})();
