(function () {
  "use strict";

  var worker = "http://127.0.0.1:8000";
  var requiredBuild = "invitation-creators-v25";
  var selectedProfile = window.__gmvSelectedProfileCode || "US_CHROME";
  var selectedStatus = "checking";
  var workerReady = false;
  var parsedItems = [];
  var parseErrors = [];
  var activeJobId = sessionStorage.getItem("invitationAcceptJobId");
  var pollTimer = null;
  var terminal = new Set(["completed", "completed_with_errors", "failed", "cancelled"]);

  function $(selector) { return document.querySelector(selector); }
  function text(node, value) { if (node) node.textContent = value == null ? "" : String(value); }

  async function json(response) {
    var payload = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(payload.detail || "요청을 처리하지 못했습니다.");
    return payload;
  }

  function setError(message) {
    var node = $("#invitation-accept-error");
    text(node, message || "");
    node.hidden = !message;
  }

  function profileLabel(code) {
    return (code.endsWith("CHROME") ? "Chrome" : "Edge") + " · " + (code.startsWith("US_") ? "US" : "UK");
  }

  function setConnection(dotSelector, labelSelector, status, label) {
    var dot = $(dotSelector);
    dot.className = "status-dot " + status;
    text($(labelSelector), label);
  }

  function parseInput(raw) {
    var tokens = String(raw || "").split(/[\r\n,]+/).map(function (value) { return value.trim(); }).filter(Boolean);
    var names = [];
    var errors = [];
    tokens.forEach(function (token) {
      var range = token.match(/^(.*?)_(\d+)~(\d+)$/);
      if (!range) { names.push(token); return; }
      var start = Number(range[2]);
      var end = Number(range[3]);
      if (start > end || end - start > 5000) { errors.push("올바르지 않은 범위: " + token); return; }
      for (var number = start; number <= end; number += 1) names.push(range[1] + "_" + number);
    });
    var seen = new Set();
    var items = [];
    names.forEach(function (name) {
      var key = name.toLowerCase();
      if (seen.has(key)) return;
      var match = name.match(/^(.*?)_([^_]+)_([^_]+)_(\d+)$/);
      if (!match || !match[1]) { errors.push("형식을 확인해주세요: " + name + " (예: PJH_SZP_0810_1)"); return; }
      seen.add(key);
      items.push({order: items.length + 1, invitation_name: name, owner: match[1], product: match[2], date: match[3], number: match[4]});
    });
    return {items: items, errors: errors};
  }

  function appendCells(row, values) {
    values.forEach(function (value) {
      var cell = document.createElement("td");
      text(cell, value);
      row.appendChild(cell);
    });
  }

  function renderPreview() {
    var parsed = parseInput($("#invitation-accept-input").value);
    parsedItems = parsed.items;
    parseErrors = parsed.errors;
    var body = $("#invitation-preview-rows");
    body.replaceChildren();
    parsedItems.forEach(function (item) {
      var row = document.createElement("tr");
      appendCells(row, [item.order, item.invitation_name, item.product, item.date, item.number]);
      body.appendChild(row);
    });
    var error = $("#invitation-parse-error");
    text(error, parseErrors.join(" / "));
    error.hidden = !parseErrors.length;
    text($("#preview-summary"), parsedItems.length ? parsedItems.length + "개 초대장 · 입력 순서 유지" : "초대장명을 입력하면 Product · Date · Number를 오른쪽 기준으로 분석합니다.");
    updateStartButton();
  }

  function updateStartButton() {
    var start = $("#start-invitation-accept");
    var running = !!activeJobId && start.dataset.terminal !== "true";
    start.disabled = !workerReady || selectedStatus !== "connected" || !parsedItems.length || !!parseErrors.length || running;
  }

  async function refreshConnections() {
    try {
      var health = await json(await fetch(worker + "/health", {cache: "no-store"}));
      workerReady = health.build === requiredBuild;
      setConnection("#accept-worker-dot", "#accept-worker-status", workerReady ? "connected" : "error", workerReady ? "Worker 연결됨" : "Worker 업데이트 필요");
    } catch (_) {
      workerReady = false;
      setConnection("#accept-worker-dot", "#accept-worker-status", "error", "Worker 연결 끊김");
    }
    if (workerReady) {
      try {
        var profiles = await json(await fetch(worker + "/profiles", {cache: "no-store"}));
        var profile = profiles.find(function (item) { return item.profile_code === selectedProfile; });
        selectedStatus = profile ? profile.status : "login_required";
      } catch (_) { selectedStatus = "error"; }
    } else selectedStatus = "error";
    var connected = selectedStatus === "connected";
    setConnection("#accept-login-dot", "#accept-login-status", connected ? "connected" : "login_required", connected ? "로그인 연결됨" : "TikTok 로그인 필요");
    text($("#accept-selected-environment"), profileLabel(selectedProfile));
    text($("#selected-login-copy"), profileLabel(selectedProfile) + (connected ? " 로그인 연결됨" : " 로그인이 필요합니다."));
    updateStartButton();
  }

  function statusLabel(status) {
    return {queued:"대기 중",running:"크리에이터 조회 중",paused:"일시정지",needs_login:"로그인 필요",cancel_requested:"중지 처리 중",cancelled:"중지됨",completed:"완료",completed_with_errors:"일부 완료",failed:"실패"}[status] || status;
  }

  function badge(status) {
    var node = document.createElement("span");
    node.className = "accept-badge status-" + String(status).toLowerCase().replace(/_/g, "-");
    text(node, status);
    return node;
  }

  function renderJob(job) {
    activeJobId = job.id;
    sessionStorage.setItem("invitationAcceptJobId", job.id);
    $("#accept-progress").hidden = false;
    text($("#accept-job-status"), statusLabel(job.status));
    text($("#accept-job-current"), job.current || "-");
    var states = job.invitation_accept_states || [];
    var success = states.filter(function (item) { return item.status === "SUCCESS"; }).length;
    var creatorRows = job.invitation_creator_rows || [];
    var already = creatorRows.length;
    var notFound = creatorRows.filter(function (item) { return item.added_products; }).length;
    var processed = states.filter(function (item) { return ["QUEUED", "PROCESSING"].indexOf(item.status) < 0; }).length;
    var errors = creatorRows.filter(function (item) { return item.posted_content; }).length;
    text($("#accept-total"), states.length);
    text($("#accept-processed"), processed);
    text($("#accept-success"), success);
    text($("#accept-already"), already);
    text($("#accept-not-found"), notFound);
    text($("#accept-errors"), errors);
    $("#accept-progress-fill").style.width = (states.length ? Math.round(processed / states.length * 100) : 0) + "%";
    text($("#accept-page-status"), job.search_page ? "Target Invitation Search Page " + job.search_page + (job.search_total_pages ? " / " + job.search_total_pages : "") : "Target Invitation Search Page -");

    var rows = $("#invitation-accept-result-rows");
    rows.replaceChildren();
    creatorRows.forEach(function (item) {
      var row = document.createElement("tr");
      appendCells(row, [
        item.keyword,
        item.invitation_name,
        item.creator,
        item.nickname || "",
        item.creator_id || "",
        item.region || item.market,
        item.added_products ? "O" : "",
        item.posted_content ? "O" : ""
      ]);
      rows.appendChild(row);
    });

    var logs = $("#invitation-accept-logs");
    logs.replaceChildren();
    (job.logs || []).slice(-150).forEach(function (entry) {
      var line = document.createElement("div");
      line.className = "accept-log-line";
      var time = document.createElement("time"); text(time, String(entry.time || "").slice(11, 19));
      line.appendChild(time); line.appendChild(badge(entry.status));
      var message = document.createElement("span"); text(message, entry.message); line.appendChild(message);
      logs.appendChild(line);
    });
    logs.scrollTop = logs.scrollHeight;

    var paused = job.status === "paused" || job.status === "needs_login";
    var finished = terminal.has(job.status);
    $("#pause-invitation-accept").hidden = paused || finished || job.status === "cancel_requested";
    $("#resume-invitation-accept").hidden = !paused;
    $("#cancel-invitation-accept").hidden = finished;
    $("#retry-invitation-accept").hidden = !finished || !states.some(function (item) { return ["SUCCESS", "ALREADY_ACCEPTED"].indexOf(item.status) < 0; });
    var download = $("#download-invitation-accept");
    download.hidden = false;
    download.href = worker + "/invitation-accept-jobs/" + job.id + "/download";
    var start = $("#start-invitation-accept");
    start.dataset.terminal = finished ? "true" : "false";
    start.textContent = finished ? "새 초대장 조회 시작" : "크리에이터 조회 중...";
    updateStartButton();
  }

  async function poll(jobId) {
    window.clearTimeout(pollTimer);
    try {
      var job = await json(await fetch(worker + "/invitation-accept-jobs/" + jobId, {cache: "no-store"}));
      renderJob(job);
      if (!terminal.has(job.status)) pollTimer = window.setTimeout(function () { poll(jobId); }, 1200);
    } catch (error) { setError(error.message); }
  }

  async function startJob() {
    setError("");
    try {
      var created = await json(await fetch(worker + "/invitation-accept-jobs", {
        method: "POST", headers: {"content-type": "application/json"},
        body: JSON.stringify({profile_code: selectedProfile, invitation_text: $("#invitation-accept-input").value})
      }));
      activeJobId = created.job_id;
      $("#start-invitation-accept").dataset.terminal = "false";
      poll(activeJobId);
    } catch (error) { setError(error.message); }
  }

  async function action(name) {
    if (!activeJobId) return;
    setError("");
    try {
      await json(await fetch(worker + "/invitation-accept-jobs/" + activeJobId + "/" + name, {method: "POST"}));
      poll(activeJobId);
    } catch (error) { setError(error.message); }
  }

  function resetPage() {
    window.clearTimeout(pollTimer);
    activeJobId = null;
    sessionStorage.removeItem("invitationAcceptJobId");
    $("#accept-progress").hidden = true;
    $("#invitation-accept-input").value = "";
    $("#start-invitation-accept").dataset.terminal = "true";
    renderPreview();
    setError("");
  }

  function install() {
    $("#invitation-accept-input").addEventListener("input", renderPreview);
    $("#start-invitation-accept").addEventListener("click", startJob);
    $("#pause-invitation-accept").addEventListener("click", function () { action("pause"); });
    $("#resume-invitation-accept").addEventListener("click", function () { action("resume"); });
    $("#cancel-invitation-accept").addEventListener("click", function () { action("cancel"); });
    $("#retry-invitation-accept").addEventListener("click", function () { action("retry"); });
    $("#reset-invitation-accept").addEventListener("click", resetPage);
    window.addEventListener("gmv-profile-change", function (event) {
      selectedProfile = event.detail.profileCode;
      selectedStatus = event.detail.status;
      refreshConnections();
    });
    renderPreview();
    refreshConnections();
    window.setInterval(refreshConnections, 3000);
    if (activeJobId) poll(activeJobId);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install);
  else install();
})();
