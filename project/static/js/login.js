(function () {
  "use strict";
  var form = document.getElementById("agentLoginForm");
  var username = document.getElementById("usernameInput");
  var password = document.getElementById("passwordInput");
  var button = document.getElementById("loginBtn");
  var errorBox = document.getElementById("loginErrorMsg");

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    errorBox.hidden = true;
    if (!username.value.trim() || !password.value) {
      showError("Enter your username and password.");
      return;
    }
    button.disabled = true;
    button.textContent = "Signing in…";
    var result = await App.api("/api/agent/login", {
      method: "POST",
      body: { username: username.value.trim(), password: password.value },
    });
    if (result.ok && result.data.success) {
      var target = result.data.must_change_password ? "/agent/change-password" : "/agent/dashboard";
      window.location.assign(target);
      return;
    }
    if (result.status === 429) {
      showError("Too many sign-in attempts. Please wait a few minutes and try again.");
    } else {
      showError(App.errorMessage(result, "Invalid username or password."));
    }
    password.value = "";
    button.disabled = false;
    button.textContent = "Sign in";
  });
})();
