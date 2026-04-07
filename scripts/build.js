/**
 * Build Script - generates static JSON files for GitHub Pages frontend.
 *
 * Reads scan results from data/, generates:
 *   site/data/latest.json    - most recent scan result
 *   site/data/history.json   - list of all scans (summary)
 *   site/data/scans/0.json   - individual scan results (0 = newest)
 *
 * Also copies public/index.html to site/index.html with API paths rewritten.
 */

const fs = require("fs");
const path = require("path");

const DATA_DIR = path.join(__dirname, "..", "data");
const SITE_DIR = path.join(__dirname, "..", "site");
const SITE_DATA_DIR = path.join(SITE_DIR, "data");
const SCANS_DIR = path.join(SITE_DATA_DIR, "scans");
const PUBLIC_DIR = path.join(__dirname, "..", "public");

// Ensure output dirs
for (const d of [SITE_DIR, SITE_DATA_DIR, SCANS_DIR]) {
  if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
}

// Read all scan files from data/, sorted newest first
const scanFiles = fs.readdirSync(DATA_DIR)
  .filter(f => f.endsWith(".json"))
  .sort()
  .reverse();

if (scanFiles.length === 0) {
  console.log("[BUILD] No scan data found, writing empty defaults.");
  const empty = { scanTime: null, totalTokens: 0, filteredTokens: 0, tokens: [] };
  fs.writeFileSync(path.join(SITE_DATA_DIR, "latest.json"), JSON.stringify(empty));
  fs.writeFileSync(path.join(SITE_DATA_DIR, "history.json"), JSON.stringify([]));
} else {
  // latest.json = most recent scan
  const latestData = JSON.parse(fs.readFileSync(path.join(DATA_DIR, scanFiles[0]), "utf-8"));
  // Sort tokens by created_at descending (newest first)
  if (latestData.tokens) {
    latestData.tokens.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  }
  fs.writeFileSync(path.join(SITE_DATA_DIR, "latest.json"), JSON.stringify(latestData));

  // history.json + individual scan files
  const history = [];
  scanFiles.forEach((file, idx) => {
    const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, file), "utf-8"));
    // Sort tokens by created_at descending (newest first)
    if (data.tokens) {
      data.tokens.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    }
    history.push({
      id: idx,
      scan_time: data.scanTime,
      total_tokens: data.totalTokens,
      filtered_tokens: data.filteredTokens,
    });
    fs.writeFileSync(path.join(SCANS_DIR, `${idx}.json`), JSON.stringify(data));
  });
  fs.writeFileSync(path.join(SITE_DATA_DIR, "history.json"), JSON.stringify(history));

  // search-index.json - deduplicated tokens across all scans for client-side search
  const seen = new Set();
  const searchIndex = [];
  scanFiles.forEach((file) => {
    const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, file), "utf-8"));
    for (const t of (data.tokens || [])) {
      const addr = t.address || '';
      if (seen.has(addr)) continue;
      seen.add(addr);
      searchIndex.push({
        a: addr,
        n: t.name || '',
        s: t.symbol || '',
        h: t.holders || 0,
        c: t.created_at || 0,
        p: t.price || 0,
        mp: t.max_price || 0,
        h2: t.high_2h || 0,
        pr: t.price_ratio,
        ah: t.age_hours,
        sc: t.social_count || 0,
        sl: t.social_links || {},
        hn: t.hot_news || false,
        hs: t.hot_score || 0,
        hk: t.hot_keywords || [],
        ts: t.total_supply || 0,
        dv: t.day1_vol || 0,
        pg: t.progress || 0,
        st: data.scanTime,
      });
    }
  });
  searchIndex.sort((a, b) => (b.c || 0) - (a.c || 0));
  fs.writeFileSync(path.join(SITE_DATA_DIR, "search-index.json"), JSON.stringify(searchIndex));
  console.log(`[BUILD] Search index: ${searchIndex.length} unique tokens.`);
  console.log(`[BUILD] Generated data for ${scanFiles.length} scans.`);
}

// Copy and patch index.html
let html = fs.readFileSync(path.join(PUBLIC_DIR, "index.html"), "utf-8");

// Rewrite API paths: /api/xxx -> data/xxx.json (relative paths for GitHub Pages)
// /api/latest       -> data/latest.json
// /api/history      -> data/history.json
// /api/scan/N       -> data/scans/N.json

html = html.replace(
  /cachedFetch\('\/api\/latest'/g,
  "cachedFetch('data/latest.json'"
);
html = html.replace(
  /cachedFetch\('\/api\/history'\)/g,
  "cachedFetch('data/history.json')"
);
html = html.replace(
  /cachedFetch\('\/api\/search-index'\)/g,
  "cachedFetch('data/search-index.json')"
);
html = html.replace(
  /cachedFetch\('\/api\/scan\/'\s*\+\s*([^)]+)\)/g,
  "cachedFetch('data/scans/' + $1 + '.json')"
);

// Inject auto-refresh polling for production (static site)
const autoRefreshCode = `
// --- Auto Refresh (polling) ---
let lastScanTime = null;
let autoRefreshTimer = null;
const AUTO_REFRESH_INTERVAL = 60 * 1000;

function startAutoRefresh() {
  if (autoRefreshTimer) return;
  autoRefreshTimer = setInterval(async () => {
    try {
      const res = await fetch('data/latest.json');
      const data = await res.json();
      if (data.scanTime && data.scanTime !== lastScanTime) {
        lastScanTime = data.scanTime;
        renderData(data);
        showToast('Data updated');
      }
    } catch(e) { console.error(e); }
  }, AUTO_REFRESH_INTERVAL);
  const btn = document.getElementById('btnAutoRefresh');
  btn.textContent = 'Auto-refresh: ON';
  btn.style.borderColor = '#f0b90b';
}

function stopAutoRefresh() {
  if (!autoRefreshTimer) return;
  clearInterval(autoRefreshTimer);
  autoRefreshTimer = null;
  const btn = document.getElementById('btnAutoRefresh');
  btn.textContent = 'Auto-refresh: OFF';
  btn.style.borderColor = '';
}

function toggleAutoRefresh() {
  autoRefreshTimer ? stopAutoRefresh() : startAutoRefresh();
}
`;

// Add auto-refresh button next to History button
html = html.replace(
  /<button id="btnHistory" onclick="toggleHistory\(\)">History<\/button>/,
  '<button id="btnHistory" onclick="toggleHistory()">History</button>\n    <button id="btnAutoRefresh" onclick="toggleAutoRefresh()" style="border-color:#f0b90b">Auto-refresh: ON</button>'
);

// Inject auto-refresh code before renderData function
html = html.replace(
  /function renderData\(data\)/,
  autoRefreshCode + '\nfunction renderData(data)'
);

// Add startAutoRefresh() to init
html = html.replace(
  /\/\/ Init\nloadLatest\(\);/,
  '// Init\nloadLatest();\nstartAutoRefresh();'
);

fs.writeFileSync(path.join(SITE_DIR, "index.html"), html);
console.log("[BUILD] Site built successfully -> site/");
