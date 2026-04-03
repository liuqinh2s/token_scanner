/**
 * BSC Token Scanner - Scan Script (for GitHub Actions)
 *
 * Fetches tokens from four.meme search API (last 3 days),
 * fetches holder counts from four.meme detail API,
 * filters by holders >= 90,
 * writes results to data/ directory as JSON files.
 */

const https = require("https");
const http = require("http");
const fs = require("fs");
const path = require("path");

// === Constants ===
const MAX_AGE_HOURS = 72;
const MIN_HOLDERS = 90;

const FM_SEARCH = "https://four.meme/meme-api/v1/public/token/search";
const FM_DETAIL = "https://four.meme/meme-api/v1/private/token/get/v2";
const FM_HEADERS = {
  "Content-Type": "application/json",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
  Accept: "application/json",
  Origin: "https://four.meme",
  Referer: "https://four.meme/",
};

const DATA_DIR = path.join(__dirname, "..", "data");

// === HTTPS Agent with Keep-Alive ===
const keepAliveAgent = new https.Agent({ keepAlive: true, maxSockets: 15 });

// === HTTP Helpers ===
function fetchJSON(url, options = {}) {
  return new Promise((resolve, reject) => {
    const mod = url.startsWith("https") ? https : http;
    const urlObj = new URL(url);
    const reqOpts = {
      hostname: urlObj.hostname,
      port: urlObj.port,
      path: urlObj.pathname + urlObj.search,
      method: options.method || "GET",
      headers: options.headers || {},
      timeout: 15000,
    };
    if (options.agent) reqOpts.agent = options.agent;
    const req = mod.request(reqOpts, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        try { resolve(JSON.parse(data)); }
        catch { resolve(null); }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
    if (options.body) req.write(options.body);
    req.end();
  });
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// === Rate Limiter ===
class RateLimiter {
  constructor(rps) {
    this.interval = Math.ceil(1000 / rps);
    this.queue = [];
    this.timer = null;
  }
  acquire() {
    return new Promise(resolve => {
      this.queue.push(resolve);
      if (!this.timer) this._start();
    });
  }
  _start() {
    this.timer = setInterval(() => {
      if (this.queue.length === 0) {
        clearInterval(this.timer);
        this.timer = null;
        return;
      }
      this.queue.shift()();
    }, this.interval);
    if (this.queue.length > 0) this.queue.shift()();
  }
}
const apiLimiter = new RateLimiter(2);

// === four.meme Search API ===
async function fmSearchTokens() {
  const maxAgeMs = MAX_AGE_HOURS * 3600 * 1000;
  const nowMs = Date.now();
  const seen = new Map(); // address -> token
  const pageSize = 100;
  const maxPages = 10;

  async function fetchPages(query, label) {
    for (let page = 1; page <= maxPages; page++) {
      const payload = { pageIndex: page, pageSize, ...query };
      try {
        await apiLimiter.acquire();
        const res = await fetchJSON(FM_SEARCH, {
          method: "POST",
          headers: FM_HEADERS,
          body: JSON.stringify(payload),
          agent: keepAliveAgent,
        });
        if (!res || res.code !== 0) break;
        const items = res.data || [];
        if (items.length === 0) break;

        for (const t of items) {
          const addr = (t.tokenAddress || "").toLowerCase();
          if (addr && !seen.has(addr)) {
            seen.set(addr, t);
          }
        }

        // Stop if we've gone past our time window (for NEW sorted queries)
        if (query.type === "NEW") {
          const oldestTs = Math.min(...items.map(i => parseInt(i.createDate || 0)));
          if (oldestTs > 0 && (nowMs - oldestTs) > maxAgeMs) break;
        }
        if (items.length < pageSize) break;
      } catch (e) {
        console.error(`[SCAN] fm_search [${label}] p${page}: ${e.message}`);
        break;
      }
      await sleep(300);
    }
  }

  const symbols = ["BNB", "USD1", "USDT", "CAKE"];

  // 1. NEW/DESC × PUBLISH × symbols
  for (const sym of symbols) {
    await fetchPages(
      { type: "NEW", listType: "NOR", sort: "DESC", status: "PUBLISH", symbol: sym },
      `NEW/DESC/PUB/${sym}`
    );
  }

  // 2. NEW/ASC × PUBLISH × symbols
  for (const sym of symbols) {
    await fetchPages(
      { type: "NEW", listType: "NOR", sort: "ASC", status: "PUBLISH", symbol: sym },
      `NEW/ASC/PUB/${sym}`
    );
  }

  // 3. NEW/DESC × TRADE × symbols
  for (const sym of symbols) {
    await fetchPages(
      { type: "NEW", listType: "NOR_DEX", sort: "DESC", status: "TRADE", symbol: sym },
      `NEW/DESC/TRADE/${sym}`
    );
  }

  // 4. NEW/ASC × TRADE × symbols
  for (const sym of symbols) {
    await fetchPages(
      { type: "NEW", listType: "NOR_DEX", sort: "ASC", status: "TRADE", symbol: sym },
      `NEW/ASC/TRADE/${sym}`
    );
  }

  // 5. HOT/VOL/PROGRESS × dual status
  for (const [sortType, listType] of [["HOT", "ADV"], ["VOL", "NOR"], ["PROGRESS", "NOR"]]) {
    for (const [status, lt] of [["PUBLISH", listType], ["TRADE", "NOR_DEX"]]) {
      await fetchPages(
        { type: sortType, listType: lt, status },
        `${sortType}/${status}`
      );
    }
  }

  console.log(`[SCAN] fm_search: fetched ${seen.size} tokens (deduplicated)`);
  return [...seen.values()];
}

// === Fetch Holder Count from Detail API ===
async function fetchHolderCount(tokenAddress) {
  await apiLimiter.acquire();
  try {
    const res = await fetchJSON(`${FM_DETAIL}?address=${tokenAddress}`, {
      headers: FM_HEADERS,
      agent: keepAliveAgent,
    });
    if (res && res.data && res.data.tokenPrice) {
      return parseInt(res.data.tokenPrice.holderCount || 0, 10);
    }
    if (res && res.data) {
      return parseInt(res.data.holderCount || res.data.hold || 0, 10);
    }
  } catch (e) { /* silent */ }
  return 0;
}

// === Main ===
async function main() {
  const scanStart = Date.now();
  const scanTime = new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }).replace(/\//g, "-");
  console.log(`\n========== SCAN START: ${scanTime} ==========`);

  // Step 1: Fetch tokens from four.meme search API
  console.log("[SCAN] Fetching tokens from four.meme API...");
  const apiTokens = await fmSearchTokens();
  console.log(`[SCAN] Found ${apiTokens.length} tokens from API`);

  // Step 2: Initial filter - age within 72h, has hold data from list API
  const nowMs = Date.now();
  const maxAgeMs = MAX_AGE_HOURS * 3600 * 1000;
  const preFiltered = apiTokens.filter(t => {
    const createDate = parseInt(t.createDate || 0);
    if (createDate <= 0) return false;
    const age = nowMs - createDate;
    return age > 0 && age <= maxAgeMs;
  });
  console.log(`[SCAN] After age filter: ${preFiltered.length} tokens`);

  // Step 3: Pre-filter by list API hold, then fetch accurate holderCount from detail API
  const candidates = preFiltered.filter(t => {
    const listHold = parseInt(t.hold || 0);
    return listHold >= MIN_HOLDERS * 0.5; // keep borderline ones too
  });
  console.log(`[SCAN] Candidates (hold>=${MIN_HOLDERS * 0.5}): ${candidates.length}`);

  // Fetch accurate holder count from detail API for all candidates
  const passed = [];
  for (let i = 0; i < candidates.length; i++) {
    const t = candidates[i];
    const holders = await fetchHolderCount(t.tokenAddress);
    if (holders >= MIN_HOLDERS) {
      passed.push({ ...t, _holders: holders });
    }
    if ((i + 1) % 50 === 0) {
      console.log(`[SCAN] Detail check: ${i + 1}/${candidates.length}`);
    }
  }
  console.log(`[SCAN] Passed after detail check: ${passed.length}/${candidates.length}`);

  // Step 4: Sort by holders descending
  const filtered = passed.sort((a, b) => b._holders - a._holders);

  const result = {
    scanTime,
    totalTokens: apiTokens.length,
    filteredTokens: filtered.length,
    tokens: filtered.map(t => ({
      address: (t.tokenAddress || "").toLowerCase(),
      name: t.name || "",
      symbol: t.shortName || t.symbol || "",
      holders: t._holders,
      created_at: parseInt(t.createDate || 0),
      total_supply: parseInt(t.totalAmount || 0) || 0,
      price: parseFloat(t.price || 0),
      day1_vol: parseFloat(t.day1Vol || 0),
      progress: parseFloat(t.progress || 0),
    })),
  };

  // Step 5: Write to data/
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

  // Generate file name in Beijing time (UTC+8)
  const bjNow = new Date(Date.now() + 8 * 3600 * 1000);
  const scanId = bjNow.toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const scanFile = path.join(DATA_DIR, `${scanId}.json`);
  fs.writeFileSync(scanFile, JSON.stringify(result, null, 2));
  console.log(`[SCAN] Wrote ${scanFile}`);

  // Step 6: Clean up data files older than 7 days
  const MAX_DATA_AGE_DAYS = 7;
  const cutoffMs = Date.now() - MAX_DATA_AGE_DAYS * 24 * 3600 * 1000;
  const dataFiles = fs.readdirSync(DATA_DIR).filter(f => f.endsWith(".json"));
  let cleaned = 0;
  for (const f of dataFiles) {
    // Parse date from filename like "2026-04-03T09-34-09.json"
    const match = f.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})\.json$/);
    if (!match) continue;
    const [, y, mo, d, h, mi, s] = match;
    const fileDate = new Date(`${y}-${mo}-${d}T${h}:${mi}:${s}Z`);
    if (fileDate.getTime() < cutoffMs) {
      fs.unlinkSync(path.join(DATA_DIR, f));
      cleaned++;
    }
  }
  if (cleaned > 0) console.log(`[SCAN] Cleaned ${cleaned} data files older than ${MAX_DATA_AGE_DAYS} days`);

  const elapsed = ((Date.now() - scanStart) / 1000).toFixed(1);
  console.log(`[SCAN] Done in ${elapsed}s. Total: ${result.totalTokens}, Filtered: ${result.filteredTokens}`);
}

main().catch(e => {
  console.error("[SCAN] Fatal error:", e);
  process.exit(1);
});
