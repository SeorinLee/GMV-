const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const webRoot = path.join(root, "web");
const scriptTag = '<script src="/app-shell.js?v=1" defer></script>';

function patchStaticHtml(relativePath) {
  const filePath = path.join(webRoot, ".next", "server", "app", relativePath);
  let source = fs.readFileSync(filePath, "utf8");
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
  if (source.includes('src:"/app-shell.js?v=1"')) return;

  const layoutMain = /([a-z])\.jsx\("main",\{className:"container",children:e\}\)\]\}\)\}\)\}\},(\d+):/;
  const match = source.match(layoutMain);
  if (!match) throw new Error(`Could not find the layout injection point in ${relativePath}`);
  const jsx = match[1];
  source = source.replace(
    layoutMain,
    `${jsx}.jsx("main",{className:"container",children:e}),${jsx}.jsx("script",{src:"/app-shell.js?v=1",defer:!0})]})})}},${match[2]}:`,
  );
  fs.writeFileSync(filePath, source, "utf8");
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

console.log("Web shell patch applied and validated.");
