const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const webRoot = path.join(root, "web");
const scriptTag = '<script src="/app-shell.js?v=8" defer></script><script src="/browser-market-selector.js?v=7" data-browser-market-selector="true" defer></script>';

function patchStaticHtml(relativePath) {
  const filePath = path.join(webRoot, ".next", "server", "app", relativePath);
  let source = fs.readFileSync(filePath, "utf8");
  source = source.replaceAll('/app-shell.js?v=1', '/app-shell.js?v=3');
  source = source.replaceAll('/app-shell.js?v=2', '/app-shell.js?v=3');
  source = source.replaceAll('/app-shell.js?v=3', '/app-shell.js?v=4');
  source = source.replaceAll('/browser-market-selector.js?v=3', '/browser-market-selector.js?v=4');
  source = source.replaceAll('/app-shell.js?v=4', '/app-shell.js?v=5');
  source = source.replaceAll('/browser-market-selector.js?v=4', '/browser-market-selector.js?v=5');
  source = source.replaceAll('/app-shell.js?v=5', '/app-shell.js?v=6');
  source = source.replaceAll('/browser-market-selector.js?v=5', '/browser-market-selector.js?v=6');
  source = source.replaceAll('/app-shell.js?v=6', '/app-shell.js?v=7');
  source = source.replaceAll('/browser-market-selector.js?v=6', '/browser-market-selector.js?v=7');
  source = source.replaceAll('/app-shell.js?v=7', '/app-shell.js?v=8');
  source = source.replaceAll(
    '<script src="/browser-market-selector.js?v=7" defer></script>',
    '<script src="/browser-market-selector.js?v=7" data-browser-market-selector="true" defer></script>',
  );
  if (source.includes('src="/app-shell.js') && !source.includes('src="/browser-market-selector.js')) {
    source = source.replace('</head>', '<script src="/browser-market-selector.js?v=7" data-browser-market-selector="true" defer></script></head>');
  }
  if (source.includes('src="/app-shell.js') && source.includes('src="/browser-market-selector.js')) {
    fs.writeFileSync(filePath, source, "utf8");
    return;
  }
  if (!source.includes('src="/app-shell.js')) {
    if (!source.includes("</head>")) {
      throw new Error(`Missing </head> in ${relativePath}`);
    }
    source = source.replace("</head>", `${scriptTag}</head>`);
    fs.writeFileSync(filePath, source, "utf8");
  }
}

function patchPageLayout(relativePath) {
  const filePath = path.join(webRoot, ".next", "server", "app", relativePath);
  let source = fs.readFileSync(filePath, "utf8");
  source = source.replaceAll('/app-shell.js?v=1', '/app-shell.js?v=3');
  source = source.replaceAll('/app-shell.js?v=2', '/app-shell.js?v=3');
  source = source.replaceAll('/app-shell.js?v=3', '/app-shell.js?v=4');
  source = source.replaceAll('/browser-market-selector.js?v=3', '/browser-market-selector.js?v=4');
  source = source.replaceAll('/app-shell.js?v=4', '/app-shell.js?v=5');
  source = source.replaceAll('/browser-market-selector.js?v=4', '/browser-market-selector.js?v=5');
  source = source.replaceAll('/app-shell.js?v=5', '/app-shell.js?v=6');
  source = source.replaceAll('/browser-market-selector.js?v=5', '/browser-market-selector.js?v=6');
  source = source.replaceAll('/app-shell.js?v=6', '/app-shell.js?v=7');
  source = source.replaceAll('/browser-market-selector.js?v=6', '/browser-market-selector.js?v=7');
  source = source.replaceAll('/app-shell.js?v=7', '/app-shell.js?v=8');
  if (source.includes('src:"/app-shell.js?v=8"')) {
    if (!source.includes('src:"/browser-market-selector.js?v=7"')) {
      source = source.replace(
        `${matchJsxVariable(source)}.jsx("script",{src:"/app-shell.js?v=8",defer:!0})`,
        `(0,${matchJsxVariable(source)}.jsxs)(${matchJsxVariable(source)}.Fragment,{children:[${matchJsxVariable(source)}.jsx("script",{src:"/app-shell.js?v=8",defer:!0}),${matchJsxVariable(source)}.jsx("script",{src:"/browser-market-selector.js?v=7",defer:!0})]})`,
      );
    }
    fs.writeFileSync(filePath, source, "utf8");
    return;
  }

  const layoutMain = /([a-z])\.jsx\("main",\{className:"container",children:e\}\)\]\}\)\}\)\}\},(\d+):/;
  const match = source.match(layoutMain);
  if (!match) throw new Error(`Could not find the layout injection point in ${relativePath}`);
  const jsx = match[1];
  source = source.replace(
    layoutMain,
    `${jsx}.jsx("main",{className:"container",children:e}),${jsx}.jsx("script",{src:"/app-shell.js?v=8",defer:!0}),${jsx}.jsx("script",{src:"/browser-market-selector.js?v=7",defer:!0})]})})}},${match[2]}:`,
  );
  fs.writeFileSync(filePath, source, "utf8");
}

function matchJsxVariable(source) {
  const match = source.match(/([a-z])\.jsx\("main",\{className:"container"/);
  if (!match) throw new Error("Could not identify JSX runtime variable");
  return match[1];
}

function patchNoStoreMetadata() {
  for (const relativePath of ["index.meta", "settings.meta", "results.meta"]) {
    const filePath = path.join(webRoot, ".next", "server", "app", relativePath);
    const metadata = JSON.parse(fs.readFileSync(filePath, "utf8"));
    metadata.headers = metadata.headers || {};
    metadata.headers["cache-control"] = "no-store, no-cache, must-revalidate";
    fs.writeFileSync(filePath, `${JSON.stringify(metadata, null, 2)}\n`, "utf8");
  }
}

function patchRoutesManifest() {
  const filePath = path.join(webRoot, ".next", "routes-manifest.json");
  const manifest = JSON.parse(fs.readFileSync(filePath, "utf8"));

  const jobActions = ["cancel", "download", "retry"];
  for (const action of jobActions) {
    const page = `/api/jobs/[id]/${action}`;
    const route = manifest.dynamicRoutes.find((item) => item.page === page);
    if (!route) throw new Error(`Missing route ${page}`);
    route.namedRegex = `^/api/jobs/(?<nxtPid>[^/]+?)/${action}(?:/)?$`;
  }

  const existingRewrites = Array.isArray(manifest.rewrites) ? manifest.rewrites : [];
  manifest.rewrites = existingRewrites.filter((item) => item.source !== "/invitations");
  manifest.rewrites.push({
    source: "/invitations",
    destination: "/invitations.html",
    regex: "^/invitations(?:/)?$",
  });
  fs.writeFileSync(filePath, `${JSON.stringify(manifest)}\n`, "utf8");
}

function patchMarketSelection() {
  const homeProfiles = 'US_CHROME:{browser:"Chrome",market:"United States",code:"US",accent:"blue",destination:"affiliate-us.tiktok.com"},UK_CHROME:{browser:"Chrome",market:"United Kingdom",code:"UK",accent:"cyan",destination:"affiliate.tiktok.com"},US_EDGE:{browser:"Edge",market:"United States",code:"US",accent:"blue",destination:"affiliate-us.tiktok.com"},UK_EDGE:{browser:"Edge",market:"United Kingdom",code:"UK",accent:"cyan",destination:"affiliate.tiktok.com"}';
  const oldHomeProfiles = [
    'US_CHROME:{browser:"Chrome",market:"United States",code:"US",accent:"blue"},UK_EDGE:{browser:"Edge",market:"United Kingdom",code:"UK",accent:"cyan"}',
    'US_CHROME:{browser:"Chrome",market:"United States",code:"US",accent:"blue",destination:"affiliate-us.tiktok.com"},UK_EDGE:{browser:"Edge",market:"United Kingdom",code:"UK",accent:"cyan",destination:"affiliate.tiktok.com"}',
  ];
  const homePaths = [
    [path.join(webRoot, ".next", "server", "app", "page.js"), "e", "n"],
    [path.join(webRoot, ".next", "static", "chunks", "app", "page-ccd9d71035d84713.js"), "s", "c"],
  ];

  for (const [filePath, codeVariable, selectedVariable] of homePaths) {
    let source = fs.readFileSync(filePath, "utf8");
    for (const oldProfiles of oldHomeProfiles) source = source.replace(oldProfiles, homeProfiles);
    source = source.replace(
      `"aria-pressed":${selectedVariable},children:`,
      `"aria-pressed":${selectedVariable},"data-profile-code":${codeVariable},children:`,
    );
    source = source.replace(
      "사용할 환경 하나만 선택하면 됩니다. Chrome 로그인만으로 US 조회가 가능합니다.",
      "Chrome 또는 Edge를 고른 뒤 US/UK 국가를 선택하세요.",
    );
    if (!source.includes(homeProfiles) || !source.includes(`"data-profile-code":${codeVariable}`)) {
      throw new Error(`Could not patch ${filePath}`);
    }
    fs.writeFileSync(filePath, source, "utf8");
  }

  const staticPath = path.join(webRoot, ".next", "server", "app", "index.html");
  let staticHtml = fs.readFileSync(staticPath, "utf8");
  staticHtml = staticHtml.replaceAll(
    "사용할 환경 하나만 선택하면 됩니다. Chrome 로그인만으로 US 조회가 가능합니다.",
    "Chrome 또는 Edge를 고른 뒤 US/UK 국가를 선택하세요.",
  );
  fs.writeFileSync(staticPath, staticHtml, "utf8");
  patchProfileSupportPages();
}

function patchProfileSupportPages() {
  const settingsProfiles = 'US_CHROME:{browser:"Chrome",market:"United States",code:"US",host:"seller-us.tiktok.com"},UK_CHROME:{browser:"Chrome",market:"United Kingdom",code:"UK",host:"seller-uk.tiktok.com"},US_EDGE:{browser:"Edge",market:"United States",code:"US",host:"seller-us.tiktok.com"},UK_EDGE:{browser:"Edge",market:"United Kingdom",code:"UK",host:"seller-uk.tiktok.com"}';
  const oldSettingsProfiles = 'US_CHROME:{browser:"Chrome",market:"United States",code:"US",host:"seller-us.tiktok.com"},UK_EDGE:{browser:"Edge",market:"United Kingdom",code:"UK",host:"seller-uk.tiktok.com"}';
  const settingsPaths = [
    path.join(webRoot, ".next", "server", "app", "settings", "page.js"),
    path.join(webRoot, ".next", "static", "chunks", "app", "settings", "page-5ce8dc730f8b8629.js"),
  ];
  for (const filePath of settingsPaths) {
    let source = fs.readFileSync(filePath, "utf8");
    source = source.replace(oldSettingsProfiles, settingsProfiles);
    source = source.replaceAll(
      "Chrome/US와 Edge/UK 로그인은 서로 분리된 브라우저 프로필에 저장됩니다.",
      "Chrome과 Edge에서 US/UK를 각각 선택할 수 있으며 로그인은 조합별로 저장됩니다.",
    );
    source = source.replaceAll(
      '"UK_EDGE"===e.profile_code?"cyan":"blue"',
      'e.profile_code.startsWith("UK_")?"cyan":"blue"',
    );
    if (!source.includes(settingsProfiles)) throw new Error(`Could not patch ${filePath}`);
    fs.writeFileSync(filePath, source, "utf8");
  }

  const environmentMap = '({US_CHROME:"Chrome · US",UK_CHROME:"Chrome · UK",US_EDGE:"Edge · US",UK_EDGE:"Edge · UK"}[e.selected_profile_code]??e.selected_profile_code)';
  const resultPaths = [
    path.join(webRoot, ".next", "server", "app", "results", "page.js"),
    path.join(webRoot, ".next", "static", "chunks", "app", "results", "page-18f4e9b72e3a0536.js"),
  ];
  for (const filePath of resultPaths) {
    let source = fs.readFileSync(filePath, "utf8");
    source = source.replace('"UK_EDGE"===e.selected_profile_code?"Edge \\xb7 UK":"Chrome \\xb7 US"', environmentMap);
    fs.writeFileSync(filePath, source, "utf8");
  }

  const jobEnvironmentMap = '({US_CHROME:"Chrome · US",UK_CHROME:"Chrome · UK",US_EDGE:"Edge · US",UK_EDGE:"Edge · UK"}[s.selected_profile_code]??s.selected_profile_code)';
  const jobPaths = [
    path.join(webRoot, ".next", "server", "app", "jobs", "[id]", "page.js"),
    path.join(webRoot, ".next", "static", "chunks", "app", "jobs", "[id]", "page-a120f84b7e8e72be.js"),
  ];
  for (const filePath of jobPaths) {
    let source = fs.readFileSync(filePath, "utf8");
    source = source.replace('"UK_EDGE"===s.selected_profile_code?"Edge \\xb7 UK":"Chrome \\xb7 US"', jobEnvironmentMap);
    fs.writeFileSync(filePath, source, "utf8");
  }
}

for (const file of ["index.html", "results.html", "settings.html", "_not-found.html"]) {
  patchStaticHtml(file);
}
for (const file of [
  "page.js",
  path.join("results", "page.js"),
  path.join("settings", "page.js"),
  path.join("jobs", "[id]", "page.js"),
  path.join("_not-found", "page.js"),
]) {
  patchPageLayout(file);
}
patchRoutesManifest();
patchMarketSelection();
patchNoStoreMetadata();

console.log("Web shell patch applied and validated.");
