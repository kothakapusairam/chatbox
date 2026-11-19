(function () {
  "use strict";
  var form = document.getElementById("changePasswordForm");
  var current = document.getElementById("currentPassword");
  var next = document.getElementById("newPassword");
  var confirm = document.getElementById("confirmPassword");
  var button = document.getElementById("saveBtn");
  var errorBox = document.getElementById("formError");

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    errorBox.hidden = true;
    if (next.value !== confirm.value) {
      errorBox.textContent = "The new passwords do not match.";
      errorBox.hidden = false;
      return;
    }
    button.disabled = true;
    var result = await App.api("/api/agent/change-password", {
      method: "POST",
      body: { current_password: current.value, new_password: next.value },
    });
    if (result.ok) {
      window.location.assign("/agent/dashboard");
      return;
    }
    errorBox.textContent = App.errorMessage(result, "Could not update your password.");
    errorBox.hidden = false;
    button.disabled = false;
  });
})();
