/**
 * Build Script - generates static JSON files for GitHub Pages frontend.
 *
 * Reads scan results from data/, generates:
 *   site/data/latest.json    - most recent scan result (含 queue/breakthrough 快照)
 *   site/data/history.json   - list of all scans (summary, 含 queue_size/breakthrough_count)
 *   site/data/scans/0.json   - individual scan results (0 = newest)
 *   site/data/search-index.json - deduplicated tokens for client-side search
 *
 * 搜索索引包含所有类型代币: 精筛/队列/已突破/淘汰/入场淘汰 (淘汰类仅供搜索)
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

// 清空 scans 目录, 避免残留过期文件 (data/ 清理后编号错位)
for (const old of fs.readdirSync(SCANS_DIR)) {
  fs.unlinkSync(path.join(SCANS_DIR, old));
}

// Read all scan files from data/, sorted newest first
const scanFiles = fs.readdirSync(DATA_DIR)
  .filter(f => f.endsWith(".json") && f !== "queue.json" && f !== "smart_money.json")
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
      queue_size: (data.queue || []).length,
      breakthrough_count: (data.breakthroughTokens || []).length,
      eliminated_count: (data.eliminatedThisRound || []).length,
      rejected_count: (data.rejectedAtEntry || []).length,
    });
    fs.writeFileSync(path.join(SCANS_DIR, `${idx}.json`), JSON.stringify(data));
  });
  fs.writeFileSync(path.join(SITE_DATA_DIR, "history.json"), JSON.stringify(history));

  // search-index.json - 包含所有时间点的代币快照，支持历史时间线查看
  // 同一地址在不同扫描时间点保留多条记录，但做采样：同一地址+来源 每30分钟最多保留一条
  // 包含所有五类代币: 精筛结果、队列存活、已突破、本轮淘汰、入场淘汰
  const searchIndex = [];
  const lastSeen = {}; // key: addr+source -> 上次记录的时间戳

  const SAMPLE_INTERVAL_MS = 14 * 60 * 1000; // 14分钟采样间隔（扫描周期15分钟，实际不丢弃任何记录）

  function addToIndex(t, source, scanTime) {
    const addr = t.address || '';
    if (!addr) return;

    // 采样: 同一地址+来源，14分钟内只保留一条（淘汰/拒绝/突破类不采样，因为只出现一次）
    if (source === 'queue' || source === 'filtered') {
      const key = addr + '|' + source;
      const scanTs = new Date(scanTime).getTime() || 0;
      if (lastSeen[key] && scanTs - lastSeen[key] < SAMPLE_INTERVAL_MS) return;
      lastSeen[key] = scanTs;
    }

    searchIndex.push({
      a: addr,
      n: t.name || '',
      s: t.symbol || '',
      h: t.holders || 0,
      c: t.created_at || 0,
      p: t.price || 0,
      mp: t.max_price || t.peak_price || 0,
      ath: t.ath || t.max_price || t.peak_price || 0,
      pr: t.price_ratio,
      ah: t.age_hours,
      sc: t.social_count || 0,
      sl: t.social_links || {},
      cc: t.copycat_count || 0,
      ic: t.is_copycat || false,
      ts: t.total_supply || 0,
      dv: t.day1_vol || 0,
      pg: t.progress || 0,
      lq: t.liquidity || 0,
      ra: t.raised_amount || 0,
      mc: t.market_cap || 0,
      ws: t.wallet_signals || [],
      st: scanTime,
      src: source,           // 来源: filtered/queue/eliminated/rejected
      rsn: t.reason || '',   // 淘汰/拒绝原因
      ph: t.peak_holders || 0,
      pp: t.peak_price || 0,
    });
  }

  // 按时间正序遍历（oldest first），这样采样保留的是最早的记录
  const scanFilesAsc = [...scanFiles].reverse();
  scanFilesAsc.forEach((file) => {
    const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, file), "utf-8"));
    const st = data.scanTime;
    for (const t of (data.tokens || [])) addToIndex(t, 'filtered', st);
    for (const t of (data.queue || [])) addToIndex(t, 'queue', st);
    for (const t of (data.breakthroughTokens || [])) addToIndex(t, 'breakthrough', st);
    for (const t of (data.eliminatedThisRound || [])) addToIndex(t, 'eliminated', st);
    for (const t of (data.rejectedAtEntry || [])) addToIndex(t, 'rejected', st);
  });
  searchIndex.sort((a, b) => (b.c || 0) - (a.c || 0));
  // 统计唯一地址数
  const uniqueAddrs = new Set(searchIndex.map(t => t.a)).size;
  fs.writeFileSync(path.join(SITE_DATA_DIR, "search-index.json"), JSON.stringify(searchIndex));
  console.log(`[BUILD] Search index: ${searchIndex.length} records, ${uniqueAddrs} unique tokens.`);
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
  /cachedFetch\('\/api\/history'/g,
  "cachedFetch('data/history.json'"
);
html = html.replace(
  /cachedFetch\('\/api\/search-index'/g,
  "cachedFetch('data/search-index.json'"
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
    if (searchMode || activeHistoryId != null) return;
    try {
      const data = await cachedFetch('data/latest.json', true);
      if (data.scanTime && data.scanTime !== lastScanTime) {
        lastScanTime = data.scanTime;
        renderData(data);
        if (isDesktop()) await renderHistoryList('historyListDesktop', true);
        if (historyVisible) await renderHistoryList('historyList', true);
        showToast('数据已更新');
      }
    } catch(e) { console.error(e); }
  }, AUTO_REFRESH_INTERVAL);
  const btn = document.getElementById('btnAutoRefresh');
  if (btn) { btn.textContent = '自动刷新: ON'; btn.style.borderColor = 'var(--accent)'; }
}

function stopAutoRefresh() {
  if (!autoRefreshTimer) return;
  clearInterval(autoRefreshTimer);
  autoRefreshTimer = null;
  const btn = document.getElementById('btnAutoRefresh');
  if (btn) { btn.textContent = '自动刷新: OFF'; btn.style.borderColor = ''; }
}

function toggleAutoRefresh() {
  autoRefreshTimer ? stopAutoRefresh() : startAutoRefresh();
}
`;

// Add auto-refresh button in actions div (match Chinese button text)
html = html.replace(
  /<button id="btnLatest"/,
  '<button id="btnAutoRefresh" onclick="toggleAutoRefresh()" style="border-color:var(--accent)">自动刷新: ON</button>\n      <button id="btnLatest"'
);

// Inject auto-refresh code before renderData function
html = html.replace(
  /function renderData\(data\)/,
  autoRefreshCode + '\nfunction renderData(data)'
);

// Add startAutoRefresh() to init
html = html.replace(
  /loadLatest\(\);\s*\n(\s*)initDesktopHistory\(\);/,
  'loadLatest();\n$1initDesktopHistory();\n$1startAutoRefresh();'
);

fs.writeFileSync(path.join(SITE_DIR, "index.html"), html);
console.log("[BUILD] Site built successfully -> site/");
