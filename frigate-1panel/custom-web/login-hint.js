(function () {
  "use strict";

  const HINT_ID = "local-frigate-password-hint";
  const HINT_TEXT = "密码至少 12 位 · 密码提示：MY";

  function isLoginPage() {
    return window.location.pathname.replace(/\/+$/, "").endsWith("/login");
  }

  function addPasswordHint() {
    if (!isLoginPage() || document.getElementById(HINT_ID)) {
      return;
    }

    const passwordInput = document.querySelector('form input[type="password"]');
    if (!passwordInput) {
      return;
    }

    const hint = document.createElement("p");
    hint.id = HINT_ID;
    hint.textContent = HINT_TEXT;
    hint.setAttribute("aria-live", "polite");
    hint.style.marginTop = "0.4rem";
    hint.style.fontSize = "0.78rem";
    hint.style.lineHeight = "1.2rem";
    hint.style.color = "hsl(var(--muted-foreground))";
    passwordInput.insertAdjacentElement("afterend", hint);
  }

  const observer = new MutationObserver(addPasswordHint);
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  addPasswordHint();
})();
