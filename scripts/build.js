/**
 * Build Script - generates static JSON files for GitHub Pages frontend.
 *
 * 数据策略: data/ 保留 7 天, 前端展示近 2 天, 其余 5 天供数据分析 (精筛成功率统计)
 *
 * Reads scan results from data/, generates:
 *   site/data/latest.json    - most recent scan result (含 queue/breakthrough 快照)
 *   site/data/history.json   - list of display-period scans (近 2 天, summary)
 *   site/data/scans/0.json   - individual scan results (近 2 天, 0 = newest)
 *   site/data/search-index.json - deduplicated tokens for client-side search (近 2 天)
 *   site/data/quality-stats.json - 精筛成功率统计 (全部 7 天, 时间跨度越长越准确)
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

// 止盈触发点 (%), 精筛成功率以此为判定标准 — 改这里即可全局生效
const TP_TRIGGER_PCT = 15;

// 漏掉的好币判定阈值: 峰值涨幅 ≥ 此百分比视为"好币"
const MISSED_THRESHOLD_PCT = 100;

// 蹭名币黑名单 (与 scanner.py FAKE_NAME_BLACKLIST 同步)
// symbol 或 name 精确匹配即排除, 小写比较
const FAKE_NAME_BLACKLIST = new Set([
  "usdt", "usdc", "busd", "dai", "tusd", "usdp", "frax", "lusd", "gusd",
  "btc", "bitcoin", "eth", "ethereum", "bnb", "sol", "solana",
  "xrp", "ripple", "ada", "cardano", "doge", "dogecoin", "shib",
  "dot", "polkadot", "avax", "avalanche", "matic", "polygon", "link",
  "uni", "uniswap", "aave", "cake", "pancakeswap",
  "wbnb", "weth", "wbtc",
  "tether", "tether usd", "binance coin", "binance-peg",
]);

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

// 分离: 近 2 天的文件用于前端展示, 全部 7 天的文件用于数据分析 (搜索索引 + 精筛成功率)
const DISPLAY_DAYS = 2;
const displayCutoff = new Date(Date.now() - DISPLAY_DAYS * 24 * 60 * 60 * 1000);

function parseFileDate(filename) {
  // 2026-04-10T12-34-56.json → Date
  try {
    const stem = filename.replace(".json", "");
    const parts = stem.split("T");
    if (parts.length !== 2) return null;
    const timePart = parts[1].replace(/-/g, ":");
    return new Date(`${parts[0]}T${timePart}Z`);
  } catch { return null; }
}

const displayFiles = scanFiles.filter(f => {
  const d = parseFileDate(f);
  return d && d >= displayCutoff;
});
const analysisOnlyFiles = scanFiles.filter(f => {
  const d = parseFileDate(f);
  return d && d < displayCutoff;
});

console.log(`[BUILD] Data files: ${scanFiles.length} total, ${displayFiles.length} for display (${DISPLAY_DAYS}d), ${analysisOnlyFiles.length} for analysis only.`);

if (scanFiles.length === 0) {
  console.log("[BUILD] No scan data found, writing empty defaults.");
  const empty = { scanTime: null, totalTokens: 0, filteredTokens: 0, tokens: [] };
  fs.writeFileSync(path.join(SITE_DATA_DIR, "latest.json"), JSON.stringify(empty));
  fs.writeFileSync(path.join(SITE_DATA_DIR, "history.json"), JSON.stringify([]));
} else {
  // latest.json = most recent scan (始终用最新的, 不受 displayFiles 限制)
  const latestData = JSON.parse(fs.readFileSync(path.join(DATA_DIR, scanFiles[0]), "utf-8"));
  // Sort tokens by created_at descending (newest first)
  if (latestData.tokens) {
    latestData.tokens.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  }
  fs.writeFileSync(path.join(SITE_DATA_DIR, "latest.json"), JSON.stringify(latestData));

  // history.json + individual scan files (仅近 2 天的数据用于前端展示)
  const history = [];
  displayFiles.forEach((file, idx) => {
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
      new_token_count: data.newDiscovered || data.totalTokens || 0,
      breakthrough_count: (data.breakthroughTokens || []).length,
      eliminated_count: (data.eliminatedThisRound || []).length,
      rejected_count: (data.rejectedAtEntry || []).length,
    });
    fs.writeFileSync(path.join(SCANS_DIR, `${idx}.json`), JSON.stringify(data));
  });
  fs.writeFileSync(path.join(SITE_DATA_DIR, "history.json"), JSON.stringify(history));

  // search-index.json - 仅近 2 天数据 (前端搜索用, 控制体积)
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
      pch1: t.price_change_h1 || 0,
      pch24: t.price_change_h24 || 0,
      bst: t.boosts || 0,
    });
  }

  // 按时间正序遍历（oldest first），这样采样保留的是最早的记录
  // 搜索索引仅用近 2 天数据, 控制前端加载体积
  const displayFilesAsc = [...displayFiles].reverse();
  displayFilesAsc.forEach((file) => {
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

  // ===== 精筛成功率统计 (使用全部 7 天数据, 时间跨度越长越准确) =====
  // 遍历所有扫描结果，收集精筛通过的代币，跟踪后续峰值价格
  // 成功 = 后续峰值价格 ≥ 精筛时价格 × (1 + TP_TRIGGER_PCT/100)
  const qualityMap = {}; // address -> { 首次精筛信息 + 后续最高价 }
  const SUCCESS_THRESHOLD = 1 + TP_TRIGGER_PCT / 100;

  // 漏掉的好币: 进过队列但未通过精筛, 峰值涨幅 ≥ MISSED_THRESHOLD_PCT
  const missedMap = {};  // address -> { 首次入队信息 + 后续最高价 }
  const filteredAddrs = new Set(); // 精筛通过过的地址
  const MISSED_THRESHOLD = 1 + MISSED_THRESHOLD_PCT / 100;

  // 正序遍历 (oldest first, 全部 7 天数据)
  const allFilesAsc = [...scanFiles].reverse();
  allFilesAsc.forEach((file) => {
    const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, file), "utf-8"));
    const st = data.scanTime;

    // 记录精筛通过的代币 (首次出现时记录精筛价格)
    // 注意: peakPrice 从精筛通过时的 price 开始算, 不用 peak_price (那是入队以来的历史最高价, 包含精筛前的涨幅)
    for (const t of (data.tokens || [])) {
      const addr = t.address || '';
      if (!addr) continue;
      filteredAddrs.add(addr);
      if (!qualityMap[addr]) {
        qualityMap[addr] = {
          address: addr,
          name: t.name || '',
          symbol: t.symbol || '',
          entryPrice: t.price || 0,
          entryTime: st,
          entryHolders: t.holders || 0,
          entryProgress: t.progress || 0,
          entryLiquidity: t.liquidity || 0,
          entryAgeHours: t.age_hours != null ? t.age_hours : null,
          peakPrice: t.price || 0,
          latestPrice: t.price || 0,
          socialLinks: t.social_links || {},
          copycatCount: t.copycat_count || 0,
          isCopycat: t.is_copycat || false,
        };
      }
    }

    // 记录队列/淘汰/突破代币到 missedMap (首次出现时记录入队价格)
    // 跳过价格异常小的记录 (< 1e-7), 这些是 DexScreener 还没数据时的链上原始价格
    const MIN_VALID_PRICE = 1e-7;
    // 价格跳变阈值: 单步涨幅超过此倍数视为内盘→外盘切换, 需重置基准价
    const PRICE_JUMP_RATIO = 50;
    const queueTokens = [
      ...(data.queue || []),
      ...(data.eliminatedThisRound || []),
      ...(data.breakthroughTokens || []),
    ];
    for (const t of queueTokens) {
      const addr = t.address || '';
      if (!addr) continue;
      const price = t.price || 0;
      if (!missedMap[addr]) {
        // 首次出现: 价格太小则先占位但标记 entryPrice=0, 等后续正常价格覆盖
        missedMap[addr] = {
          address: addr,
          name: t.name || '',
          symbol: t.symbol || '',
          entryPrice: price >= MIN_VALID_PRICE ? price : 0,
          entryTime: st,
          entryHolders: t.holders || 0,
          entryProgress: t.progress || 0,
          entryLiquidity: t.liquidity || 0,
          entryAgeHours: t.age_hours != null ? t.age_hours : null,
          peakPrice: price >= MIN_VALID_PRICE ? price : 0,
          latestPrice: price >= MIN_VALID_PRICE ? price : 0,
          peakHolders: t.holders || 0,
          socialLinks: t.social_links || {},
          copycatCount: t.copycat_count || 0,
          isCopycat: t.is_copycat || false,
          elimReason: t.reason || '',
        };
      } else {
        const m = missedMap[addr];
        // 更新最高持币数
        if ((t.holders || 0) > m.peakHolders) m.peakHolders = t.holders || 0;
        // 之前占位的记录 (entryPrice=0), 现在有了正常价格, 更新为真实入队价格
        if (m.entryPrice === 0 && price >= MIN_VALID_PRICE) {
          m.entryPrice = price;
          m.entryTime = st;
          m.entryHolders = t.holders || 0;
          m.entryProgress = t.progress || 0;
          m.entryLiquidity = t.liquidity || 0;
          m.entryAgeHours = t.age_hours != null ? t.age_hours : null;
          m.peakPrice = price;
          m.latestPrice = price;
          if (t.reason) m.elimReason = t.reason;
        }
        // 检测内盘→外盘价格跳变: 当前价格比已知最高价高 50 倍以上, 重置基准价
        if (price >= MIN_VALID_PRICE && m.entryPrice > 0 && m.peakPrice > 0 && price > m.peakPrice * PRICE_JUMP_RATIO) {
          m.entryPrice = price;
          m.entryTime = st;
          m.entryHolders = t.holders || 0;
          m.peakPrice = price;
          m.latestPrice = price;
        }
      }
    }

    // 更新所有已记录代币的峰值价格 (仅用实时 price, 不用 peak_price — 后者包含精筛前的历史最高价)
    const allTokens = [
      ...(data.tokens || []),
      ...(data.queue || []),
      ...(data.breakthroughTokens || []),
    ];
    for (const t of allTokens) {
      const addr = t.address || '';
      if (!addr) continue;
      const price = t.price || 0;
      // 更新 qualityMap
      if (qualityMap[addr]) {
        if (price > qualityMap[addr].peakPrice) qualityMap[addr].peakPrice = price;
        qualityMap[addr].latestPrice = price;
      }
      // 更新 missedMap (含跳变检测)
      if (missedMap[addr] && price >= MIN_VALID_PRICE) {
        const m = missedMap[addr];
        if ((t.holders || 0) > m.peakHolders) m.peakHolders = t.holders || 0;
        // 检测价格跳变: 重置基准价
        if (m.entryPrice > 0 && m.peakPrice > 0 && price > m.peakPrice * PRICE_JUMP_RATIO) {
          m.entryPrice = price;
          m.entryTime = st;
          m.entryHolders = t.holders || 0;
          m.peakPrice = price;
        } else if (price > m.peakPrice) {
          m.peakPrice = price;
        }
        m.latestPrice = price;
      }
    }
  });

  // 分类: 成功 vs 失败
  const successTokens = [];
  const failTokens = [];

  for (const [addr, info] of Object.entries(qualityMap)) {
    if (info.entryPrice <= 0) continue;
    const peakGrowth = (info.peakPrice - info.entryPrice) / info.entryPrice;
    const latestGrowth = (info.latestPrice - info.entryPrice) / info.entryPrice;
    const item = {
      ...info,
      peakGrowth: Math.round(peakGrowth * 10000) / 100,   // 百分比, 保留2位
      latestGrowth: Math.round(latestGrowth * 10000) / 100,
    };
    if (info.peakPrice >= info.entryPrice * SUCCESS_THRESHOLD) {
      successTokens.push(item);
    } else {
      failTokens.push(item);
    }
  }

  // 按峰值涨幅降序排列
  successTokens.sort((a, b) => b.peakGrowth - a.peakGrowth);
  failTokens.sort((a, b) => b.peakGrowth - a.peakGrowth);

  // 漏掉的好币: 进过队列但从未通过精筛, 且峰值涨幅 ≥ MISSED_THRESHOLD_PCT
  // 额外要求: 队列期间最高持币数 ≥ 10 (排除无人关注的垃圾币)
  // 排除蹭名币 (symbol/name 命中主流币种黑名单)
  const MISSED_MIN_PEAK_HOLDERS = 10;
  const missedTokens = [];
  for (const [addr, info] of Object.entries(missedMap)) {
    if (filteredAddrs.has(addr)) continue;
    if (info.entryPrice <= 0) continue;
    if (info.peakHolders < MISSED_MIN_PEAK_HOLDERS) continue;
    const sym = (info.symbol || '').trim().toLowerCase();
    const nm = (info.name || '').trim().toLowerCase();
    if (FAKE_NAME_BLACKLIST.has(sym) || FAKE_NAME_BLACKLIST.has(nm)) continue;
    const peakGrowth = (info.peakPrice - info.entryPrice) / info.entryPrice;
    const latestGrowth = (info.latestPrice - info.entryPrice) / info.entryPrice;
    if (info.peakPrice < info.entryPrice * MISSED_THRESHOLD) continue;
    missedTokens.push({
      ...info,
      peakGrowth: Math.round(peakGrowth * 10000) / 100,
      latestGrowth: Math.round(latestGrowth * 10000) / 100,
    });
  }
  missedTokens.sort((a, b) => b.peakGrowth - a.peakGrowth);

  const totalQuality = successTokens.length + failTokens.length;
  const successRate = totalQuality > 0 ? Math.round(successTokens.length / totalQuality * 10000) / 100 : 0;

  const qualityStats = {
    tpTriggerPct: TP_TRIGGER_PCT,
    missedThresholdPct: MISSED_THRESHOLD_PCT,
    totalCount: totalQuality,
    successCount: successTokens.length,
    failCount: failTokens.length,
    missedCount: missedTokens.length,
    successRate: successRate,
    successTokens: successTokens,
    failTokens: failTokens,
    missedTokens: missedTokens,
  };

  fs.writeFileSync(path.join(SITE_DATA_DIR, "quality-stats.json"), JSON.stringify(qualityStats));
  console.log(`[BUILD] Quality stats: ${totalQuality} tokens, ${successTokens.length} success (${successRate}%), ${failTokens.length} fail, ${missedTokens.length} missed (≥${MISSED_THRESHOLD_PCT}%). (7-day data)`);
  console.log(`[BUILD] Generated: ${displayFiles.length} scans for display, ${scanFiles.length} scans for analysis.`);
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
  /cachedFetch\('\/api\/quality-stats'/g,
  "cachedFetch('data/quality-stats.json'"
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
  /loadLatest\(\);\s*\n(\s*)initDesktopHistory\(\);\s*\n(\s*)loadQualityStats\(\);/,
  'loadLatest();\n$1initDesktopHistory();\n$2loadQualityStats();\n$2startAutoRefresh();'
);

fs.writeFileSync(path.join(SITE_DIR, "index.html"), html);
console.log("[BUILD] Site built successfully -> site/");
