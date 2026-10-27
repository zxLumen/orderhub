(() => {
  "use strict";
  const form = document.getElementById("loginForm");
  const password = document.getElementById("password");
  const error = document.getElementById("loginError");
  const button = document.getElementById("loginBtn");

  function showError(message) {
    error.textContent = message;
    error.classList.remove("hidden");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.classList.add("hidden");
    button.disabled = true;
    button.textContent = "登录中…";
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: password.value }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        showError(data.error === "rate_limited" ? "尝试次数太多，请稍后再试。" : "密码不正确。");
        password.select();
        return;
      }
      const next = new URLSearchParams(location.search).get("next") || "/";
      location.replace(next.startsWith("/") && !next.startsWith("//") ? next : "/");
    } catch (_) {
      showError("网络不可用，请稍后重试。");
    } finally {
      button.disabled = false;
      button.textContent = "登录";
    }
  });
})();
