(function () {
  "use strict";

  function addStylesheet() {
    if (document.querySelector('link[data-app-shell-style]')) return;
    var link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "/app-shell.css?v=5";
    link.setAttribute("data-app-shell-style", "true");
    document.head.appendChild(link);
  }

  function addBrowserMarketSelector() {
    if (window.location.pathname !== "/" && window.location.pathname.indexOf("/settings") !== 0) return;
    if (document.querySelector('script[data-browser-market-selector]')) return;
    var script = document.createElement("script");
    script.src = "/browser-market-selector.js?v=7";
    script.defer = true;
    script.setAttribute("data-browser-market-selector", "true");
    document.head.appendChild(script);
  }

  function currentSection() {
    return window.location.pathname.indexOf("/invitations") === 0
      ? "invitations"
      : "gmv";
  }

  function updateActiveMenu(shell) {
    var section = currentSection();
    shell.querySelectorAll("[data-automation-section]").forEach(function (link) {
      var active = link.getAttribute("data-automation-section") === section;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function createShell() {
    var existing = document.querySelector(".automation-shell");
    if (existing) return existing;

    var shell = document.createElement("header");
    shell.className = "automation-shell";
    shell.innerHTML =
      '<div class="automation-shell-inner">' +
      '<a class="automation-brand" href="/" aria-label="TikTok Automation 홈">' +
      '<span class="automation-brand-mark" aria-hidden="true">TA</span>' +
      '<span>TikTok Automation</span>' +
      "</a>" +
      '<nav class="automation-nav" aria-label="자동화 메뉴">' +
      '<a href="/" data-automation-section="gmv">GMV 자동화</a>' +
      '<a href="/invitations" data-automation-section="invitations">초대장 조회</a>' +
      "</nav>" +
      "</div>";

    document.body.insertBefore(shell, document.body.firstChild);
    return shell;
  }

  function install() {
    addStylesheet();
    addBrowserMarketSelector();
    var shell = createShell();
    updateActiveMenu(shell);
    document.documentElement.classList.add("app-shell-ready");

    window.addEventListener("popstate", function () {
      updateActiveMenu(shell);
    });
    document.addEventListener("click", function (event) {
      if (event.target && event.target.closest("a")) {
        window.setTimeout(function () {
          updateActiveMenu(shell);
        }, 0);
      }
    });
  }

  addStylesheet();
  if (document.readyState === "complete") window.setTimeout(install, 50);
  else window.addEventListener("load", function () { window.setTimeout(install, 50); }, { once: true });
})();
