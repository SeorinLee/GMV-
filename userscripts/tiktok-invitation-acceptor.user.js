// ==UserScript==
// @name         TikTok Shop Invitation Acceptor (Exact Match Safe)
// @namespace    local.tiktok.automation
// @version      1.0.0
// @description  Target Invitation을 입력 순서대로 exact match 검증 후 수락합니다.
// @match        https://affiliate-us.tiktok.com/*
// @match        https://affiliate-uk.tiktok.com/*
// @match        https://affiliate.tiktok.com/*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @require      https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js
// ==/UserScript==

(function () {
  "use strict";
  var KEY = "tiktokInvitationAcceptJobV1";
  var TARGET_PATH = "/affiliate/collaboration/target-invitation";
  var networkNames = new Set();

  function normalize(value) { return String(value || "").trim().toLowerCase(); }
  function exact(actual, requested) { return normalize(actual) === normalize(requested); }
  function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
  function now() { return new Date().toISOString(); }

  function parse(raw) {
    var names = [];
    var errors = [];
    String(raw || "").split(/[\r\n,]+/).map(function (v) { return v.trim(); }).filter(Boolean).forEach(function (token) {
      var range = token.match(/^(.*?)_(\d+)~(\d+)$/);
      if (!range) { names.push(token); return; }
      var start = Number(range[2]); var end = Number(range[3]);
      if (start > end || end - start > 5000) { errors.push("Invalid range: " + token); return; }
      for (var n = start; n <= end; n += 1) names.push(range[1] + "_" + n);
    });
    var seen = new Set();
    var items = [];
    names.forEach(function (name) {
      if (seen.has(normalize(name))) return;
      var match = name.match(/^(.*?)_([^_]+)_([^_]+)_(\d+)$/);
      if (!match || !match[1]) { errors.push("Invalid format: " + name); return; }
      seen.add(normalize(name));
      items.push({order: items.length + 1, fullName: name, owner: match[1], product: match[2], date: match[3], number: match[4], status: "QUEUED", message: "", processedAt: ""});
    });
    return {items: items, errors: errors};
  }

  function inspectJson(value) {
    var stack = [value];
    while (stack.length) {
      var item = stack.pop();
      if (Array.isArray(item)) { stack.push.apply(stack, item); continue; }
      if (!item || typeof item !== "object") continue;
      Object.keys(item).forEach(function (key) {
        var child = item[key];
        var folded = key.replace(/_/g, "").toLowerCase();
        if ((folded === "invitationname" || folded === "targetinvitationname") && typeof child === "string") networkNames.add(child.trim());
        else if (child && typeof child === "object") stack.push(child);
      });
    }
  }

  var nativeFetch = window.fetch;
  window.fetch = async function () {
    var response = await nativeFetch.apply(this, arguments);
    response.clone().json().then(inspectJson).catch(function () {});
    return response;
  };
  var nativeOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function () {
    this.addEventListener("load", function () { try { inspectJson(JSON.parse(this.responseText)); } catch (_) {} });
    return nativeOpen.apply(this, arguments);
  };

  function save(job) { GM_setValue(KEY, job); }
  function load() { return GM_getValue(KEY, null); }
  function log(job, message, status) {
    job.logs.push({time: now(), message: message, status: status || "INFO"});
    if (job.logs.length > 500) job.logs = job.logs.slice(-500);
    save(job);
    console.log("[Invitation Acceptor]", message);
  }

  async function waitForElement(find, timeout) {
    var found = find(); if (found) return found;
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () { observer.disconnect(); reject(new Error("Element timeout")); }, timeout || 10000);
      var observer = new MutationObserver(function () { var node = find(); if (node) { clearTimeout(timer); observer.disconnect(); resolve(node); } });
      observer.observe(document.documentElement, {childList: true, subtree: true});
    });
  }

  async function retryAction(action, retries) {
    var last;
    for (var attempt = 0; attempt <= (retries || 2); attempt += 1) {
      try { return await action(); } catch (error) { last = error; await sleep(250 * (attempt + 1)); }
    }
    throw last;
  }

  function visible(node) { return !!node && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length); }
  function buttons(scope, pattern) { return Array.from(scope.querySelectorAll("button,[role=button]")).filter(function (node) { return visible(node) && pattern.test(node.textContent.trim()); }); }
  function searchInput() { return Array.from(document.querySelectorAll("input")).find(function (node) { var hint = ((node.placeholder || "") + " " + (node.getAttribute("aria-label") || "")).toLowerCase(); return visible(node) && (hint.includes("invitation") || hint.includes("search") || hint.includes("초대")); }); }
  function setInput(input, value) { var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; setter.call(input, value); input.dispatchEvent(new Event("input", {bubbles:true})); input.dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", code:"Enter", bubbles:true})); input.dispatchEvent(new KeyboardEvent("keyup", {key:"Enter", code:"Enter", bubbles:true})); }

  async function nextPage() {
    var next = Array.from(document.querySelectorAll("button,[role=button]")).find(function (node) {
      var label = ((node.getAttribute("aria-label") || "") + " " + node.textContent).trim();
      return visible(node) && /^(next|다음|>)$/i.test(label) && !node.disabled && node.getAttribute("aria-disabled") !== "true";
    });
    if (!next) return false;
    next.click(); await sleep(350); return true;
  }

  async function findExact(item, job) {
    var input = await waitForElement(searchInput, 12000);
    setInput(input, item.product); await sleep(500);
    var visited = new Set();
    for (var page = 1; page <= 100; page += 1) {
      job.searchPage = page; log(job, "Page " + page + " · " + item.fullName + " exact match 확인", "PROCESSING");
      var candidates = Array.from(document.querySelectorAll("body *")).filter(function (node) { return node.children.length === 0 && visible(node) && exact(node.textContent, item.fullName); });
      if (candidates.length) return candidates[0];
      var signature = document.body.innerText.slice(-3000); if (visited.has(signature)) break; visited.add(signature);
      if (!(await nextPage())) break;
    }
    throw Object.assign(new Error("모든 페이지에서 exact match 없음"), {code:"NOT_FOUND"});
  }

  async function acceptOne(item, job) {
    var node = await findExact(item, job);
    if (!exact(node.textContent, item.fullName)) throw Object.assign(new Error("Exact match safety gate"), {code:"DETAIL_FAILED"});
    var row = node.closest("tr,[role=row],[class*=card]");
    var view = row && buttons(row, /^(view|details|보기|상세)$/i)[0];
    (view || node).click(); await sleep(400);
    var scope = Array.from(document.querySelectorAll('[role=dialog],[class*=drawer],[class*=modal],[class*=detail]')).filter(visible).pop() || document;
    var identity = Array.from(scope.querySelectorAll("*")).some(function (candidate) { return candidate.children.length === 0 && visible(candidate) && exact(candidate.textContent, item.fullName); });
    if (!identity) throw Object.assign(new Error("상세 Full Name 검증 실패"), {code:"DETAIL_FAILED"});
    if (Array.from(scope.querySelectorAll("*")).some(function (n) { return n.children.length === 0 && visible(n) && /^(accepted|joined|completed|수락 완료|수락됨)$/i.test(n.textContent.trim()); })) return {status:"ALREADY_ACCEPTED", message:"이미 수락됨"};
    var accept = buttons(scope, /^(accept|accept invitation|join|수락|참여)$/i)[0];
    if (!accept) throw Object.assign(new Error("Accept button not found"), {code:"ACCEPT_BUTTON_NOT_FOUND"});
    if (!identity || !exact(item.fullName, node.textContent)) throw new Error("Exact match safety gate blocked click");
    accept.click(); await sleep(180);
    var modal = Array.from(document.querySelectorAll('[role=dialog],[class*=modal]')).filter(visible).pop();
    if (modal && modal !== scope) {
      var confirm = buttons(modal, /^(confirm|accept|accept invitation|join|확인|수락)$/i)[0];
      if (!confirm) throw Object.assign(new Error("Confirm button not found"), {code:"CONFIRM_FAILED"});
      confirm.click();
    }
    for (var i = 0; i < 40; i += 1) {
      if (!visible(accept) || /accepted|joined|success|수락.*완료/i.test(document.body.innerText)) return {status:"SUCCESS", message:"수락 완료 검증"};
      await sleep(250);
    }
    throw Object.assign(new Error("Accept verification failed"), {code:"ACCEPT_VERIFY_FAILED"});
  }

  async function run() {
    var job = load(); if (!job || job.status !== "running") return;
    if (location.origin !== job.origin) { alert("선택한 Market과 현재 페이지가 다릅니다."); return; }
    if (location.pathname !== TARGET_PATH) { location.assign(location.origin + TARGET_PATH); return; }
    for (var index = job.index; index < job.items.length; index += 1) {
      job = load(); if (!job || job.status !== "running") return;
      var item = job.items[index]; if (["SUCCESS","ALREADY_ACCEPTED"].includes(item.status)) continue;
      item.status = "PROCESSING"; job.index = index; save(job);
      try { var result = await retryAction(function () { return acceptOne(item, job); }, 2); item.status = result.status; item.message = result.message; }
      catch (error) { item.status = error.code || "UNKNOWN_ERROR"; item.message = error.message; }
      item.processedAt = now(); job.index = index + 1; save(job);
      location.assign(location.origin + TARGET_PATH); return;
    }
    job.status = "completed"; job.finishedAt = now(); log(job, "전체 작업 완료", "COMPLETED");
  }

  function start() {
    var raw = prompt("수락할 초대장명을 줄바꿈 또는 쉼표로 입력하세요."); if (!raw) return;
    var parsed = parse(raw); if (parsed.errors.length) { alert(parsed.errors.join("\n")); return; }
    if (!parsed.items.length || !confirm(parsed.items.length + "개 초대장을 exact match 검증 후 수락합니다. 계속할까요?")) return;
    var job = {status:"running", origin:location.origin, requestedOrder:parsed.items.map(function (i) { return i.fullName; }), items:parsed.items, index:0, searchPage:0, logs:[], startedAt:now(), finishedAt:""};
    save(job); location.assign(location.origin + TARGET_PATH);
  }

  function exportXlsx() {
    var job = load(); if (!job) return;
    var rows = job.items.map(function (i) { return {Order:i.order, Invitation:i.fullName, Product:i.product, Date:i.date, Number:i.number, Market:job.origin, Status:i.status, Message:i.message, "Processed At":i.processedAt}; });
    var errors = rows.filter(function (r) { return !["SUCCESS","ALREADY_ACCEPTED","QUEUED","PROCESSING"].includes(r.Status); }).map(function (r) { return {Order:r.Order, Invitation:r.Invitation, Error:r.Message || r.Status, Time:r["Processed At"]}; });
    var book = XLSX.utils.book_new(); XLSX.utils.book_append_sheet(book, XLSX.utils.json_to_sheet(rows), "Results"); XLSX.utils.book_append_sheet(book, XLSX.utils.json_to_sheet(errors), "Errors"); XLSX.writeFile(book, "invitation_accept_results.xlsx");
  }

  GM_registerMenuCommand("초대장 수락 시작", start);
  GM_registerMenuCommand("일시정지", function () { var job=load(); if(job){job.status="paused";save(job);} });
  GM_registerMenuCommand("작업 계속", function () { var job=load(); if(job){job.status="running";save(job);run();} });
  GM_registerMenuCommand("작업 중지", function () { var job=load(); if(job){job.status="cancelled";save(job);} });
  GM_registerMenuCommand("결과 XLSX 다운로드", exportXlsx);
  GM_registerMenuCommand("작업 초기화", function () { if(confirm("저장된 작업을 초기화할까요?")) GM_deleteValue(KEY); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run); else run();
})();
