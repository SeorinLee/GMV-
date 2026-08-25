(function () {
  "use strict";

  if (window.__gmvBrowserMarketSelectorLoaded) return;
  window.__gmvBrowserMarketSelectorLoaded = true;

  var selectedBrowser = "CHROME";
  var selectedMarket = "US";
  var profiles = [];
  var originalFetch = window.fetch.bind(window);

  function selectedCode() {
    return selectedMarket + "_" + selectedBrowser;
  }

  function browserName(code) {
    return code.endsWith("CHROME") ? "Chrome" : "Edge";
  }

  function marketName(code) {
    return code.startsWith("US_") ? "United States" : "United Kingdom";
  }

  function marketCode(code) {
    return code.startsWith("US_") ? "US" : "UK";
  }

  function profileStatus(code) {
    var profile = profiles.find(function (item) { return item.profile_code === code; });
    return profile ? profile.status : "checking";
  }

  function statusText(status) {
    return {
      checking: "연결 확인 중",
      connected: "로그인 연결됨",
      login_required: "로그인 필요",
      disconnected: "로그인 필요",
      expired: "로그인 만료",
      connecting: "연결 중",
      running: "작업 중",
      error: "연결 오류"
    }[status] || status;
  }

  async function loadProfiles() {
    try {
      var response = await originalFetch("/api/profiles", { cache: "no-store" });
      if (response.ok) profiles = await response.json();
    } catch (_) {}
  }

  function installJobProfileOverride() {
    if (window.__gmvProfileFetchOverride) return;
    window.__gmvProfileFetchOverride = true;
    window.fetch = function (input, init) {
      var url = typeof input === "string" ? input : input && input.url;
      if (url === "/api/jobs" && init && init.method === "POST" && init.body instanceof FormData) {
        init.body.set("profile_code", selectedCode());
        // One submitted Job must open one automation page/window. Starting another Job from a
        // second site tab creates another independent Worker runtime and browser window.
        init.body.set("concurrency", "1");
      }
      return originalFetch(input, init);
    };
  }

  function chooseLegacySource(grid) {
    var wantedBrowser = selectedBrowser === "CHROME" ? "Chrome" : "Edge";
    Array.prototype.some.call(grid.querySelectorAll(".profile-option"), function (button) {
      var strong = button.querySelector("strong");
      if (strong && strong.textContent.trim() === wantedBrowser) {
        if (!button.classList.contains("selected")) button.click();
        return true;
      }
      return false;
    });
  }

  function syncHome(picker, grid) {
    grid.parentNode.querySelectorAll(".inline-alert:not(.profile-login-helper)").forEach(function (alert) {
      alert.style.display = "none";
    });
    picker.querySelectorAll("[data-browser]").forEach(function (button) {
      var active = button.getAttribute("data-browser") === selectedBrowser;
      button.classList.toggle("selected", active);
      button.setAttribute("aria-pressed", String(active));
    });
    picker.querySelectorAll("[data-market]").forEach(function (button) {
      var active = button.getAttribute("data-market") === selectedMarket;
      button.classList.toggle("selected", active);
      button.setAttribute("aria-pressed", String(active));
    });

    var code = selectedCode();
    window.__gmvSelectedProfileCode = code;
    var status = profileStatus(code);
    var host = selectedMarket === "US" ? "affiliate-us.tiktok.com" : "affiliate.tiktok.com";
    picker.querySelector("[data-selection-summary]").textContent =
      browserName(code) + " · " + selectedMarket + " · " + host;
    var statusNode = picker.querySelector("[data-selection-status]");
    statusNode.textContent = statusText(status);
    statusNode.className = "picker-status " + status;

    var actionLabel = document.querySelector(".action-bar strong");
    if (actionLabel) actionLabel.textContent = browserName(code) + " · " + selectedMarket;
    if (window.location.pathname === "/") {
      var fileReady = !!document.querySelector(".upload-zone.has-file");
      var submit = document.querySelector(".action-bar .btn.primary");
      if (submit) submit.disabled = !(fileReady && status === "connected");
    }
    window.dispatchEvent(new CustomEvent("gmv-profile-change", {
      detail: { profileCode: code, status: status }
    }));

    var oldAlert = grid.parentNode.querySelector(".profile-login-helper");
    if (oldAlert) oldAlert.remove();
    if (["login_required", "disconnected", "expired", "error"].indexOf(status) >= 0) {
      var helper = document.createElement("p");
      helper.className = "inline-alert profile-login-helper";
      helper.innerHTML = "선택한 조합의 로그인이 필요합니다. <a href=\"/settings?ui=profile-matrix-v5\">로그인 관리 열기 →</a>";
      grid.parentNode.appendChild(helper);
    }
  }

  function createPicker(grid) {
    var picker = document.createElement("div");
    picker.className = "browser-market-picker";
    picker.innerHTML =
      '<div class="picker-group"><span class="picker-label">1. 브라우저 선택</span>' +
      '<div class="picker-options" role="group" aria-label="브라우저 선택">' +
      '<button type="button" data-browser="CHROME"><strong>Chrome</strong><small>Google Chrome</small></button>' +
      '<button type="button" data-browser="EDGE"><strong>Edge</strong><small>Microsoft Edge</small></button></div></div>' +
      '<div class="picker-arrow" aria-hidden="true">→</div>' +
      '<div class="picker-group"><span class="picker-label">2. 국가 선택</span>' +
      '<div class="picker-options market-options" role="group" aria-label="국가 선택">' +
      '<button type="button" data-market="US"><span class="picker-code us">US</span><strong>United States</strong></button>' +
      '<button type="button" data-market="UK"><span class="picker-code uk">UK</span><strong>United Kingdom</strong></button></div></div>' +
      '<div class="picker-selection"><span>선택한 환경</span><strong data-selection-summary></strong>' +
      '<small data-selection-status class="picker-status checking"></small></div>';
    picker.addEventListener("click", function (event) {
      var browser = event.target.closest("[data-browser]");
      var market = event.target.closest("[data-market]");
      if (browser) selectedBrowser = browser.getAttribute("data-browser");
      if (market) selectedMarket = market.getAttribute("data-market");
      if (!browser && !market) return;
      chooseLegacySource(grid);
      syncHome(picker, grid);
    });
    return picker;
  }

  function installHome() {
    var grid = document.querySelector(".profile-grid");
    if (!grid) return;
    var description = grid.closest(".step-card").querySelector(".step-heading p");
    if (description) description.textContent = "Chrome 또는 Edge를 고른 뒤 US/UK 국가를 선택하세요. 선택 조합 그대로 자동화됩니다.";
    grid.classList.add("profile-combo-source");
    var picker = document.querySelector(".browser-market-picker");
    if (!picker) {
      picker = createPicker(grid);
      grid.parentNode.insertBefore(picker, grid);
    }
    syncHome(picker, grid);
  }

  async function profileAction(code, action, errorNode) {
    errorNode.textContent = "";
    try {
      var response = await originalFetch("/api/profiles/" + code + "/" + action, { method: "POST" });
      if (!response.ok) {
        var payload = await response.json().catch(function () { return {}; });
        errorNode.textContent = payload.detail || "요청을 처리하지 못했습니다.";
      }
    } catch (_) {
      errorNode.textContent = "Worker 연결 상태를 확인하세요.";
    }
    window.setTimeout(refreshSettings, 800);
  }

  function profileCard(profile) {
    var code = profile.profile_code;
    var section = document.createElement("section");
    section.className = "account-card custom-account-card";
    section.setAttribute("data-custom-profile", code);
    section.innerHTML =
      '<div class="account-card-head"><span class="market-mark ' +
      (code.startsWith("UK_") ? "cyan" : "blue") + '">' + marketCode(code) + '</span>' +
      '<div><h2>' + browserName(code) + " · " + marketName(code) + '</h2><p>' +
      (code.startsWith("US_") ? "seller-us.tiktok.com" : "seller-uk.tiktok.com") + '</p></div>' +
      '<span class="status-pill ' + profile.status + '">' + statusText(profile.status) + '</span></div>' +
      '<div class="account-times"><div><span>마지막 로그인</span><strong>' +
      (profile.last_login_at || "-") + '</strong></div><div><span>마지막 확인</span><strong>' +
      (profile.last_verified_at || "-") + '</strong></div></div>' +
      '<p class="inline-alert error custom-profile-error"></p>' +
      '<div class="row"><button class="btn primary" data-action="login">' + browserName(code) + ' 로그인</button>' +
      '<button class="btn secondary" data-action="verify">연결 확인</button>' +
      '<button class="btn ghost" data-action="reset">세션 초기화</button></div>';
    var errorNode = section.querySelector(".custom-profile-error");
    section.addEventListener("click", function (event) {
      var button = event.target.closest("[data-action]");
      if (button) profileAction(code, button.getAttribute("data-action"), errorNode);
    });
    return section;
  }

  async function refreshSettings() {
    await loadProfiles();
    var sourceGrid = document.querySelector(".settings-grid:not(.profile-matrix-grid)");
    if (!sourceGrid || !profiles.length) return;
    sourceGrid.style.display = "none";
    var grid = document.querySelector(".profile-matrix-grid");
    if (!grid) {
      grid = document.createElement("div");
      grid.className = "settings-grid custom-settings-grid profile-matrix-grid";
      sourceGrid.parentNode.insertBefore(grid, sourceGrid);
    }
    grid.replaceChildren();
    profiles.forEach(function (profile) { grid.appendChild(profileCard(profile)); });
    var intro = document.querySelector(".page-intro .sub");
    if (intro) intro.textContent = "Chrome과 Edge에서 US/UK를 각각 선택할 수 있으며 로그인은 조합별로 저장됩니다.";
  }

  async function start() {
    installJobProfileOverride();
    async function refreshCurrentPage() {
      if (window.location.pathname === "/" || window.location.pathname.indexOf("/invitations") === 0) {
        await loadProfiles();
        installHome();
      } else if (window.location.pathname.indexOf("/settings") === 0) {
        await refreshSettings();
      }
    }
    await refreshCurrentPage();
    window.setInterval(refreshCurrentPage, 1000);
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest('a[href="/settings"]');
    if (!link || window.location.pathname.indexOf("/settings") === 0) return;
    event.preventDefault();
    event.stopPropagation();
    window.location.assign("/settings?ui=profile-matrix-v5");
  }, true);

  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest('a[href="/"]');
    if (!link || window.location.pathname === "/") return;
    event.preventDefault();
    event.stopPropagation();
    window.location.assign("/?ui=browser-market-v5");
  }, true);

  start();
})();
