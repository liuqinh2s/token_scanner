/**
 * BSC Token Scanner - Scan Script (for GitHub Actions)
 *
 * 三级筛选管线:
 *   Stage 1 (初筛): 币龄、当前价、持币地址数 — 仅用 search API 批量数据，0 额外请求
 *   Stage 2 (详情筛): 总量=10亿、社交媒体≥1 — four.meme detail API，每候选 1 请求
 *   Stage 3 (K线筛): 历史最高价、前2h最高价、当前价/最高价比 — GeckoTerminal OHLCV，每候选 1~2 请求
 *
 * 逐级收窄，避免不必要的 API 调用。
 */

const https = require("https");
const http = require("http");
const fs = require("fs");
const path = require("path");

// === Proxy Config ===
// 读取项目根目录 proxy.json，本地调试时启用代理（已在 .gitignore 中排除）
const PROXY_CONFIG_PATH = path.join(__dirname, "..", "proxy.json");
let proxyConfig = null;
try {
  if (fs.existsSync(PROXY_CONFIG_PATH)) {
    proxyConfig = JSON.parse(fs.readFileSync(PROXY_CONFIG_PATH, "utf-8"));
    if (proxyConfig.enabled) {
      console.log(`[PROXY] 已启用代理: ${proxyConfig.host}:${proxyConfig.port}`);
    } else {
      proxyConfig = null;
    }
  }
} catch (e) {
  console.warn(`[PROXY] 读取 proxy.json 失败: ${e.message}`);
  proxyConfig = null;
}

// === Constants ===
const MAX_AGE_HOURS = 72;

// Filter thresholds
const TOTAL_SUPPLY = 1_000_000_000;       // 10亿
const MAX_CURRENT_PRICE_OLD = 0.00002;    // 币龄 > 1h 当前价格上限 (USD)
const MAX_CURRENT_PRICE_YOUNG = 0.000004; // 币龄 ≤ 1h 当前价格上限 (USD)
const MAX_HIGH_PRICE = 0.00004;           // 历史最高价上限 (USD)
const MAX_EARLY_HIGH_PRICE = 0.00002;     // 前2小时最高价上限 (USD, 币龄>2h时检查)
const PRICE_RATIO_LOW = 0.1;              // 当前价 ≥ 最高价 * 10%
const PRICE_RATIO_HIGH = 0.8;             // 当前价 ≤ 最高价 * 80%
const HOLDERS_THRESHOLD_OLD = 60;         // 币龄 > 1h 时持币地址数阈值
const HOLDERS_THRESHOLD_YOUNG = 30;       // 币龄 ≤ 1h 时持币地址数阈值
const MIN_SOCIAL_COUNT = 1;               // 最少关联社交媒体数

// API endpoints
const FM_SEARCH = "https://four.meme/meme-api/v1/public/token/search";
const FM_DETAIL = "https://four.meme/meme-api/v1/private/token/get/v2";
const GT_BASE   = "https://api.geckoterminal.com/api/v2";

const FM_HEADERS = {
  "Content-Type": "application/json",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
  Accept: "application/json",
  Origin: "https://four.meme",
  Referer: "https://four.meme/",
};
const GT_HEADERS = {
  Accept: "application/json",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
};

const DATA_DIR = path.join(__dirname, "..", "data");

// === Timestamped Logging ===
// 给所有日志加上北京时间戳，方便分析性能瓶颈
function _ts() {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(11, 23).replace("T", " ");
}
const _origLog = console.log.bind(console);
const _origWarn = console.warn.bind(console);
const _origErr = console.error.bind(console);
console.log = (...args) => _origLog(`[${_ts()}]`, ...args);
console.warn = (...args) => _origWarn(`[${_ts()}]`, ...args);
console.error = (...args) => _origErr(`[${_ts()}]`, ...args);

// ===================================================================
//  热点数据层
//  从微博热搜 / Google Trends / Twitter(X) 抓取实时热点关键词,
//  与代币名称/描述做交叉匹配 (加分项, 匹配的代币额外标注)
// ===================================================================
const HOTSPOT_CACHE_TTL = 900_000; // 15 分钟缓存
let hotspotCache = { ts: 0, keywords: [] };

/** 文本归一化: 小写 + 去特殊符号 */
function normalize(text) {
  return text.toLowerCase().trim().replace(/[_\-./·・\s]+/g, " ");
}

/** 微博实时热搜 Top50 */
async function fetchWeiboHot() {
  try {
    const res = await fetchJSON("https://weibo.com/ajax/side/hotSearch", {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        Referer: "https://weibo.com/",
        "X-Requested-With": "XMLHttpRequest",
        Accept: "application/json",
      },
      timeout: 10000,
    });
    if (!res.data || !res.data.data) return [];
    const items = res.data.data.realtime || [];
    const results = [];
    for (let i = 0; i < items.length; i++) {
      const word = (items[i].word || "").trim();
      if (word && word.length >= 2) {
        results.push({ word, rank: i, source: "weibo" });
      }
    }
    console.log(`[HOTSPOT] 微博热搜: ${results.length} 个关键词`);
    return results;
  } catch (e) {
    console.warn(`[HOTSPOT] 微博热搜获取失败: ${e.message}`);
    return [];
  }
}

/** Google Trends 每日热门搜索 (RSS, 多地区) */
async function fetchGoogleTrends(geos = ["US", "CN"]) {
  const results = [];
  const seen = new Set();
  for (const geo of geos) {
    try {
      const res = await fetchJSON(`https://trends.google.com/trending/rss?geo=${geo}`, {
        headers: { "User-Agent": "Mozilla/5.0", Accept: "application/xml" },
        timeout: 10000,
      });
      // RSS 返回的是 XML, fetchJSON 会解析失败, 需要用原始文本
      // 改用简单正则提取 <title> 标签
    } catch (e) { /* handled below */ }

    // 直接用 http(s) 获取原始 XML
    try {
      const xml = await new Promise((resolve, reject) => {
        const mod = https;
        const urlObj = new URL(`https://trends.google.com/trending/rss?geo=${geo}`);
        const reqOpts = {
          hostname: urlObj.hostname,
          path: urlObj.pathname + urlObj.search,
          headers: { "User-Agent": "Mozilla/5.0" },
          timeout: 10000,
        };
        if (proxyConfig) {
          // 走代理隧道
          const connectReq = http.request({
            host: proxyConfig.host, port: proxyConfig.port,
            method: "CONNECT", path: `${urlObj.hostname}:443`, timeout: 10000,
          });
          connectReq.on("connect", (res, socket) => {
            if (res.statusCode !== 200) return reject(new Error("proxy connect failed"));
            const req = mod.request({ ...reqOpts, socket, agent: false }, (r) => {
              let d = ""; r.on("data", c => d += c); r.on("end", () => resolve(d));
            });
            req.on("error", reject);
            req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
            req.end();
          });
          connectReq.on("error", reject);
          connectReq.end();
        } else {
          const req = mod.request(reqOpts, (r) => {
            let d = ""; r.on("data", c => d += c); r.on("end", () => resolve(d));
          });
          req.on("error", reject);
          req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
          req.end();
        }
      });
      // 从 XML 中提取 <item><title>...</title></item>
      const titleMatches = xml.match(/<item>[\s\S]*?<title>([^<]+)<\/title>/g) || [];
      for (let i = 0; i < titleMatches.length; i++) {
        const m = titleMatches[i].match(/<title>([^<]+)<\/title>/);
        if (!m) continue;
        const word = m[1].trim();
        if (word && word.length >= 2 && !seen.has(word.toLowerCase())) {
          seen.add(word.toLowerCase());
          results.push({ word, rank: i, source: `google/${geo}` });
        }
      }
    } catch (e) {
      console.warn(`[HOTSPOT] Google Trends [${geo}] 获取失败: ${e.message}`);
    }
    await sleep(300);
  }
  console.log(`[HOTSPOT] Google Trends: ${results.length} 个关键词`);
  return results;
}

/** Twitter/X 热门话题 (via getdaytrends.com) */
async function fetchTwitterTrending() {
  try {
    const html = await new Promise((resolve, reject) => {
      const urlObj = new URL("https://getdaytrends.com/united-states/");
      const reqOpts = {
        hostname: urlObj.hostname,
        path: urlObj.pathname,
        headers: { "User-Agent": "Mozilla/5.0", Accept: "text/html" },
        timeout: 10000,
      };
      if (proxyConfig) {
        const connectReq = http.request({
          host: proxyConfig.host, port: proxyConfig.port,
          method: "CONNECT", path: `${urlObj.hostname}:443`, timeout: 10000,
        });
        connectReq.on("connect", (res, socket) => {
          if (res.statusCode !== 200) return reject(new Error("proxy connect failed"));
          const req = https.request({ ...reqOpts, socket, agent: false }, (r) => {
            let d = ""; r.on("data", c => d += c); r.on("end", () => resolve(d));
          });
          req.on("error", reject);
          req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
          req.end();
        });
        connectReq.on("error", reject);
        connectReq.end();
      } else {
        const req = https.request(reqOpts, (r) => {
          let d = ""; r.on("data", c => d += c); r.on("end", () => resolve(d));
        });
        req.on("error", reject);
        req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
        req.end();
      }
    });
    const matches = html.match(/href="\/united-states\/trend\/[^"]*">([^<]+)<\/a>/g) || [];
    const results = [];
    const seen = new Set();
    for (let i = 0; i < matches.length; i++) {
      const m = matches[i].match(/>([^<]+)<\/a>/);
      if (!m) continue;
      let word = m[1].trim().replace(/^#/, "");
      if (word && word.length >= 2 && !seen.has(word.toLowerCase())) {
        seen.add(word.toLowerCase());
        results.push({ word, rank: i, source: "twitter" });
      }
    }
    console.log(`[HOTSPOT] Twitter Trending: ${results.length} 个关键词`);
    return results;
  } catch (e) {
    console.warn(`[HOTSPOT] Twitter Trending 获取失败: ${e.message}`);
    return [];
  }
}

/** 汇总所有热点关键词 (带缓存) */
async function fetchAllHotspots() {
  const now = Date.now();
  if (now - hotspotCache.ts < HOTSPOT_CACHE_TTL && hotspotCache.keywords.length > 0) {
    console.log(`[HOTSPOT] 使用缓存: ${hotspotCache.keywords.length} 个关键词`);
    return hotspotCache.keywords;
  }
  const all = [];
  all.push(...await fetchWeiboHot());
  all.push(...await fetchGoogleTrends(["US", "CN"]));
  all.push(...await fetchTwitterTrending());
  console.log(`[HOTSPOT] 热点汇总: ${all.length} 个关键词`);
  hotspotCache = { ts: now, keywords: all };
  return all;
}

/**
 * 代币与热点关键词匹配
 * 匹配逻辑:
 *   - 短关键词 (≤3字符) 要求精确匹配 name 或 shortName
 *   - 长关键词: 子串包含匹配
 *   - 反向匹配: 代币名包含在热点词中 (如代币 "张雪" 匹配热点 "张雪机车")
 *   - 按热点排名和来源加权评分
 */
function hotspotMatch(token, hotspots, descr = "") {
  const name = normalize(token.name || "");
  const short = normalize(token.shortName || "");
  const desc = normalize(descr);

  let score = 0;
  const matched = [];
  const seenWords = new Set();

  for (const h of hotspots) {
    const wordLower = normalize(h.word);
    if (seenWords.has(wordLower)) continue;

    // 短关键词 (≤3字符) 要求精确匹配 name 或 shortName
    if (wordLower.length <= 3) {
      if (wordLower !== name && wordLower !== short) continue;
    } else {
      let found = false;
      // 正向: 热点词 ⊂ 代币字段
      if (name.includes(wordLower) || short.includes(wordLower)) {
        found = true;
      } else if (desc && desc.includes(wordLower)) {
        found = true;
      }
      // 反向: 代币名 ⊂ 热点词 (如代币 "张雪" 匹配热点 "张雪机车")
      if (!found && name.length >= 2) {
        if (wordLower.includes(name) || wordLower.includes(short)) {
          found = true;
        }
      }
      if (!found) continue;
    }

    seenWords.add(wordLower);
    // 排名权重: rank=0 → 1.0, rank=49 → 0.5
    const rankWeight = Math.max(0.5, 1.0 - h.rank * 0.01);
    // 来源权重: 微博略高 (中文 meme 币与中文热点相关性更强)
    const srcBase = h.source.split("/")[0];
    const sourceWeight = { weibo: 1.2, twitter: 1.0 }[srcBase] || 0.9;
    score += rankWeight * sourceWeight;
    matched.push(`${h.word}(${h.source})`);
  }
  return { score, matched, isHot: matched.length > 0 };
}

// === HTTPS Agents ===
const fmAgent = new https.Agent({ keepAlive: true, maxSockets: 15 });
const gtAgent = new https.Agent({ keepAlive: true, maxSockets: 5 });

// === HTTP Helper (with optional proxy support) ===
function fetchJSON(url, options = {}) {
  return new Promise((resolve, reject) => {
    const urlObj = new URL(url);
    const isHttps = url.startsWith("https");
    const requestTimeout = options.timeout || 30000;

    function doRequest(socket) {
      const mod = isHttps ? https : http;
      const reqOpts = {
        hostname: urlObj.hostname,
        port: urlObj.port || (isHttps ? 443 : 80),
        path: urlObj.pathname + urlObj.search,
        method: options.method || "GET",
        headers: options.headers || {},
        timeout: requestTimeout,
      };
      if (socket) {
        reqOpts.socket = socket;
        reqOpts.agent = false; // 使用已建立的 tunnel socket，不走 agent
      } else if (options.agent) {
        reqOpts.agent = options.agent;
      }
      const req = mod.request(reqOpts, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try { resolve({ status: res.statusCode, data: JSON.parse(data) }); }
          catch { resolve({ status: res.statusCode, data: null }); }
        });
      });
      req.on("error", reject);
      req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
      if (options.body) req.write(options.body);
      req.end();
    }

    // 如果启用了代理，通过 HTTP CONNECT 建立隧道
    if (proxyConfig && isHttps) {
      const connectReq = http.request({
        host: proxyConfig.host,
        port: proxyConfig.port,
        method: "CONNECT",
        path: `${urlObj.hostname}:${urlObj.port || 443}`,
        timeout: requestTimeout,
      });
      connectReq.on("connect", (res, socket) => {
        if (res.statusCode === 200) {
          doRequest(socket);
        } else {
          reject(new Error(`Proxy CONNECT failed: ${res.statusCode}`));
        }
      });
      connectReq.on("error", reject);
      connectReq.on("timeout", () => { connectReq.destroy(); reject(new Error("proxy timeout")); });
      connectReq.end();
    } else {
      doRequest(null);
    }
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
const fmLimiter = new RateLimiter(5);   // four.meme ~5 req/s (网络延迟是瓶颈, 非速率)
const gtLimiter = new RateLimiter(1);   // GeckoTerminal ~30 req/min, 直接用 tokenAddr 省掉 getPool
let gtRateDelay = 2000; // 动态退避, 初始 2s

// ===================================================================
//  four.meme Search API
// ===================================================================
async function fmSearchTokens() {
  const maxAgeMs = MAX_AGE_HOURS * 3600 * 1000;
  const nowMs = Date.now();
  const seen = new Map();
  const pageSize = 100;
  const maxPages = 10;

  async function fetchPages(query, label) {
    for (let page = 1; page <= maxPages; page++) {
      const payload = { pageIndex: page, pageSize, ...query };
      try {
        await fmLimiter.acquire();
        const res = await fetchJSON(FM_SEARCH, {
          method: "POST",
          headers: FM_HEADERS,
          body: JSON.stringify(payload),
          agent: fmAgent,
        });
        if (!res.data || res.data.code !== 0) break;
        const items = res.data.data || [];
        if (items.length === 0) break;

        for (const t of items) {
          const addr = (t.tokenAddress || "").toLowerCase();
          if (addr && !seen.has(addr)) seen.set(addr, t);
        }

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

  // 不同查询之间并发 (同一 fetchPages 内分页仍串行)
  // JS 单线程, seen Map 并发安全
  const allQueries = [];

  const symbols = ["BNB", "USD1", "USDT", "CAKE"];
  for (const sym of symbols) {
    allQueries.push({ query: { type: "NEW", listType: "NOR", sort: "DESC", status: "PUBLISH", symbol: sym }, label: `NEW/DESC/PUB/${sym}` });
  }
  for (const sym of symbols) {
    allQueries.push({ query: { type: "NEW", listType: "NOR", sort: "ASC", status: "PUBLISH", symbol: sym }, label: `NEW/ASC/PUB/${sym}` });
  }
  for (const sym of symbols) {
    allQueries.push({ query: { type: "NEW", listType: "NOR_DEX", sort: "DESC", status: "TRADE", symbol: sym }, label: `NEW/DESC/TRADE/${sym}` });
  }
  for (const sym of symbols) {
    allQueries.push({ query: { type: "NEW", listType: "NOR_DEX", sort: "ASC", status: "TRADE", symbol: sym }, label: `NEW/ASC/TRADE/${sym}` });
  }
  for (const [sortType, listType] of [["HOT", "ADV"], ["VOL", "NOR"], ["PROGRESS", "NOR"]]) {
    for (const [status, lt] of [["PUBLISH", listType], ["TRADE", "NOR_DEX"]]) {
      allQueries.push({ query: { type: sortType, listType: lt, status }, label: `${sortType}/${status}` });
    }
  }

  // 并发池: 同时跑 SEARCH_CONCURRENCY 组查询
  const SEARCH_CONCURRENCY = 4;
  for (let i = 0; i < allQueries.length; i += SEARCH_CONCURRENCY) {
    const batch = allQueries.slice(i, i + SEARCH_CONCURRENCY);
    await Promise.all(batch.map(({ query, label }) => fetchPages(query, label)));
  }

  console.log(`[SCAN] fm_search: fetched ${seen.size} tokens (deduplicated)`);
  return [...seen.values()];
}

// ===================================================================
//  four.meme Detail API
// ===================================================================
async function fetchTokenDetail(tokenAddress) {
  await fmLimiter.acquire();
  try {
    const res = await fetchJSON(`${FM_DETAIL}?address=${tokenAddress}`, {
      headers: FM_HEADERS,
      agent: fmAgent,
    });
    if (!res.data || !res.data.data) return null;
    const d = res.data.data;
    const tp = d.tokenPrice || {};

    const socialLinks = {};
    if (d.twitterUrl) socialLinks.twitter = d.twitterUrl;
    if (d.telegramUrl) socialLinks.telegram = d.telegramUrl;
    if (d.webUrl) socialLinks.website = d.webUrl;

    return {
      holders: parseInt(tp.holderCount || 0, 10),
      price: parseFloat(tp.price || 0),
      totalSupply: parseInt(d.totalAmount || 0, 10),
      socialCount: Object.keys(socialLinks).length,
      socialLinks,
      descr: d.descr || "",
    };
  } catch (e) { /* silent */ }
  return null;
}

// ===================================================================
//  DexScreener API (主要) — 300 req/min, 比 GeckoTerminal 快 10 倍
// ===================================================================
const DS_BASE = "https://api.dexscreener.com";
const DS_HEADERS = { Accept: "application/json", "User-Agent": "Mozilla/5.0" };

/** 通过 DexScreener 获取代币的交易对信息 */
async function dsGetPairs(tokenAddress) {
  try {
    const res = await fetchJSON(`${DS_BASE}/tokens/v1/bsc/${tokenAddress}`, {
      headers: DS_HEADERS,
      timeout: 10000,
    });
    if (res.status === 429) {
      await sleep(2000);
      const retry = await fetchJSON(`${DS_BASE}/tokens/v1/bsc/${tokenAddress}`, {
        headers: DS_HEADERS,
        timeout: 10000,
      });
      return Array.isArray(retry.data) ? retry.data : (retry.data?.pairs || []);
    }
    const result = Array.isArray(res.data) ? res.data : (res.data?.pairs || res.data?.data || []);
    if (result.length === 0) console.log(`[DS] ${tokenAddress.slice(0, 16)}: 无交易对 (Bonding Curve?)`);
    return result;
  } catch (e) {
    console.error(`[DS] dsGetPairs [${tokenAddress.slice(0, 16)}]: ${e.message}`);
    return null;
  }
}

/**
 * 从 DexScreener pair 数据提取价格信息。
 * 返回 { ath, high2h, currentPrice } 或 null
 * DexScreener 没有直接的 OHLCV, 用 priceChange 反推历史高点。
 */
function dsExtractPrices(pairs) {
  if (!pairs || pairs.length === 0) return null;
  for (const pair of pairs) {
    if (pair.chainId && pair.chainId !== "bsc") continue;
    const priceUsd = parseFloat(pair.priceUsd || 0);
    if (!priceUsd) continue;

    let maxPrice = priceUsd;
    const pc = pair.priceChange || {};

    // 用各时间段变化率反推历史高点
    for (const key of ["m5", "h1", "h6", "h24"]) {
      if (pc[key] != null) {
        const pct = parseFloat(pc[key]);
        if (pct < 0) {
          // 价格下跌了, 之前更高
          maxPrice = Math.max(maxPrice, priceUsd / (1 + pct / 100));
        }
      }
    }

    return { ath: maxPrice, high2h: maxPrice, currentPrice: priceUsd };
  }
  return null;
}

// ===================================================================
//  GeckoTerminal API (备选) — K线 OHLCV, ~30 req/min
// ===================================================================
async function gtRequest(url, maxRetries = 3) {
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      await gtLimiter.acquire();
      const res = await fetchJSON(url, {
        headers: GT_HEADERS,
        agent: gtAgent,
        timeout: 15000,
      });
      if (res.status === 429) {
        const wait = 5000 * (attempt + 1);
        gtRateDelay = Math.min(5000, gtRateDelay + 1000);
        console.warn(`[GT] 429 rate limited, waiting ${wait}ms (${attempt + 1}/${maxRetries})`);
        await sleep(wait);
        continue;
      }
      gtRateDelay = Math.max(500, gtRateDelay - 200);
      return res.data;
    } catch (e) {
      if (attempt < maxRetries - 1) await sleep(3000);
      else console.error(`[GT] request failed: ${e.message}`);
    }
  }
  return null;
}

async function gtGetPool(tokenAddress) {
  const data = await gtRequest(`${GT_BASE}/networks/bsc/tokens/${tokenAddress}`);
  if (!data) return null;
  const pools = ((data.data || {}).relationships || {}).top_pools || {};
  const poolList = pools.data || [];
  return poolList.length > 0 ? poolList[0].id.replace("bsc_", "") : null;
}

async function gtOhlcvHourly(poolAddress, limit = 72) {
  const url = `${GT_BASE}/networks/bsc/pools/${poolAddress}/ohlcv/hour?aggregate=1&limit=${limit}`;
  const data = await gtRequest(url);
  if (!data) return [];
  return ((data.data || {}).attributes || {}).ohlcv_list || [];
}

/** 直接用 tokenAddress 当 poolAddress 拿 K线, 省掉 gtGetPool 请求 */
async function gtOhlcvDirect(tokenAddress, limit = 72) {
  return gtOhlcvHourly(tokenAddress, limit);
}

/** 从 OHLCV K线计算历史最高价 (USD) */
function calcAllTimeHigh(candles) {
  if (!candles || candles.length === 0) return null;
  return Math.max(...candles.map(c => parseFloat(c[2])));
}

/** 从 OHLCV K线计算发币后前 N 小时内的最高价 (USD) */
function calcMaxPriceFirstNHours(candles, createTsSec, hours = 2) {
  if (!candles || candles.length === 0) return null;
  const cutoff = createTsSec + hours * 3600;
  let maxHigh = 0;
  let found = false;
  for (const c of candles) {
    const ts = parseInt(c[0]);
    if (ts > cutoff) continue;
    if (ts < createTsSec - 3600) continue;
    const high = parseFloat(c[2]);
    if (high > maxHigh) maxHigh = high;
    found = true;
  }
  return found ? maxHigh : null;
}

// (hotspot matching is now handled by hotspotMatch above)

// ===================================================================
//  三级筛选管线
// ===================================================================

/** Stage 1: 初筛 — 仅用 search API 批量数据, 0 额外请求 */
function stage1_prefilter(tokens, nowMs) {
  const maxAgeMs = MAX_AGE_HOURS * 3600 * 1000;
  return tokens.filter(t => {
    const createDate = parseInt(t.createDate || 0);
    if (createDate <= 0) return false;
    const ageMs = nowMs - createDate;
    if (ageMs <= 0 || ageMs > maxAgeMs) return false;

    // 当前价初筛 (宽松阈值, 保留边界候选)
    const price = parseFloat(t.price || 0);
    if (price > MAX_CURRENT_PRICE_OLD) return false;

    // 持币地址数初筛 (宽松阈值, 保留边界候选)
    const hold = parseInt(t.hold || 0);
    const ageHours = ageMs / (3600 * 1000);
    const minHold = ageHours > 1 ? HOLDERS_THRESHOLD_OLD * 0.5 : HOLDERS_THRESHOLD_YOUNG * 0.5;
    if (hold < minHold) return false;

    return true;
  });
}

/** Stage 2: 详情筛 — four.meme detail API, 每候选 1 请求 */
async function stage2_detail(candidates, nowMs) {
  const results = [];
  const CONCURRENCY = 5; // 并发数, rate limiter 仍控制实际速率

  async function processOne(t) {
    const detail = await fetchTokenDetail(t.tokenAddress);
    if (!detail) return null;

    const createDate = parseInt(t.createDate || 0);
    const ageHours = (nowMs - createDate) / (3600 * 1000);

    // 社交媒体 ≥ 1
    if (detail.socialCount < MIN_SOCIAL_COUNT) return null;

    // 持币地址数 (按币龄区分阈值)
    if (ageHours > 1 && detail.holders < HOLDERS_THRESHOLD_OLD) return null;
    if (ageHours <= 1 && detail.holders < HOLDERS_THRESHOLD_YOUNG) return null;

    // 总量 = 10亿
    if (detail.totalSupply !== TOTAL_SUPPLY) return null;

    // 当前价: 币龄≤1h → ≤0.000004, 币龄>1h → ≤0.00002
    const maxPrice = ageHours > 1 ? MAX_CURRENT_PRICE_OLD : MAX_CURRENT_PRICE_YOUNG;
    if (detail.price > maxPrice) return null;

    return { token: t, detail, ageHours };
  }

  // 并发池: 同时发起 CONCURRENCY 个请求, rate limiter 控制节奏
  for (let i = 0; i < candidates.length; i += CONCURRENCY) {
    const batch = candidates.slice(i, i + CONCURRENCY);
    const batchResults = await Promise.all(batch.map(t => processOne(t)));
    for (const r of batchResults) {
      if (r) results.push(r);
    }
    if (i + CONCURRENCY < candidates.length) {
      console.log(`[SCAN] Stage2: ${Math.min(i + CONCURRENCY, candidates.length)}/${candidates.length}, passed: ${results.length}`);
    }
  }
  return results;
}

/** Stage 3: K线筛 — DexScreener (现价) + GeckoTerminal (K线), DS+GT 并行请求 */
async function stage3_kline(candidates, hotspots) {
  const CONCURRENCY = 5;

  // 对每个候选: DS 拿现价 + GT 直接用 tokenAddress 拿 K线, 两者并行
  async function fetchAllData(candidate) {
    const addr = candidate.token.tokenAddress;
    const createTsSec = parseInt(candidate.token.createDate || 0) / 1000;

    // DS 和 GT 并行
    const [dsPairs, candles] = await Promise.all([
      dsGetPairs(addr),
      gtOhlcvDirect(addr, 72),
    ]);

    // DS: 提取现价
    let dsCurrentPrice = null;
    if (dsPairs && dsPairs.length > 0) {
      for (const pair of dsPairs) {
        if (pair.chainId && pair.chainId !== "bsc") continue;
        const p = parseFloat(pair.priceUsd || 0);
        if (p > 0) { dsCurrentPrice = p; break; }
      }
    }

    // GT: 从 K线算 ATH 和 2h高
    let ath = null, high2h = null, gtCurrentPrice = null;
    if (candles && candles.length > 0) {
      high2h = calcMaxPriceFirstNHours(candles, createTsSec, 2);
      ath = calcAllTimeHigh(candles);
      const latestCandle = candles.reduce((a, b) => (parseInt(a[0]) > parseInt(b[0]) ? a : b));
      gtCurrentPrice = parseFloat(latestCandle[4]);
    }

    return { candidate, dsCurrentPrice, ath, high2h, gtCurrentPrice };
  }

  // 并发池
  const allData = [];
  for (let i = 0; i < candidates.length; i += CONCURRENCY) {
    const batch = candidates.slice(i, i + CONCURRENCY);
    const batchResults = await Promise.all(batch.map(c => fetchAllData(c)));
    allData.push(...batchResults);
  }

  // 筛选
  const results = [];
  for (const { candidate, dsCurrentPrice, ath: rawAth, high2h, gtCurrentPrice } of allData) {
    const { token: t, detail, ageHours } = candidate;
    const addr = t.tokenAddress;
    const name = t.name || addr.slice(0, 16);

    let ath = rawAth;
    const currentPrice = dsCurrentPrice || gtCurrentPrice;

    if (ath === null && high2h === null) {
      console.log(`[SCAN] Stage3: ${name} (${addr}) — 无K线数据, 跳过`);
      continue;
    }

    if (ath === null) ath = high2h;

    console.log(`[SCAN] Stage3: ${name} (${addr}) — ATH ${(ath||0).toExponential(3)}, 2h高 ${(high2h||0).toExponential(3)}, 现价 ${(currentPrice||0).toExponential(3)}`);

    if (ath > MAX_HIGH_PRICE) {
      console.log(`[SCAN] Stage3: ${name} (${addr}) — ATH ${ath.toExponential(3)} > ${MAX_HIGH_PRICE}, 跳过`);
      continue;
    }

    if (ageHours > 2 && high2h !== null && high2h > MAX_EARLY_HIGH_PRICE) {
      console.log(`[SCAN] Stage3: ${name} (${addr}) — 前2h最高 ${high2h.toExponential(3)} > ${MAX_EARLY_HIGH_PRICE}, 跳过`);
      continue;
    }

    if (ageHours >= 1 && ath > 0 && currentPrice) {
      const ratio = currentPrice / ath;
      if (ratio < PRICE_RATIO_LOW || ratio > PRICE_RATIO_HIGH) {
        console.log(`[SCAN] Stage3: ${name} (${addr}) — 现/高 ${(ratio * 100).toFixed(1)}% 不在 10%~80%, 跳过`);
        continue;
      }
    }

    const hotNews = hotspotMatch(t, hotspots, detail.descr);

    results.push({ token: t, detail, ageHours, ath, high2h, hotNews, dsCurrentPrice: currentPrice });
    const finalPrice = currentPrice || 0;
    console.log(`[SCAN] Stage3: ✓ ${name} — ATH ${ath.toExponential(3)}, 2h高 ${(high2h || 0).toExponential(3)}, 现/高 ${ath > 0 ? (finalPrice / ath * 100).toFixed(1) : '?'}%${hotNews.isHot ? ' 🔥' + hotNews.matched.join(',') : ''}`);
  }
  return results;
}

// ===================================================================
//  Main
// ===================================================================
async function main() {
  const scanStart = Date.now();
  const scanTime = new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }).replace(/\//g, "-");
  console.log(`\n========== SCAN START: ${scanTime} ==========`);

  // Step 1: Fetch tokens from four.meme search API
  console.log("[SCAN] Fetching tokens from four.meme API...");
  const apiTokens = await fmSearchTokens();
  console.log(`[SCAN] Found ${apiTokens.length} tokens from API`);

  const nowMs = Date.now();

  // Stage 1: 初筛
  console.log(`[SCAN] Stage1 初筛条件: 币龄≤${MAX_AGE_HOURS}h, 当前价≤$${MAX_CURRENT_PRICE_OLD}, 持币≥(>1h:${HOLDERS_THRESHOLD_OLD * 0.5}, ≤1h:${HOLDERS_THRESHOLD_YOUNG * 0.5})`);
  const s1 = stage1_prefilter(apiTokens, nowMs);
  console.log(`[SCAN] Stage1 初筛: ${s1.length}/${apiTokens.length}`);

  // Stage 2 + 热点抓取并行
  console.log(`[SCAN] Stage2 详情筛条件: 社交媒体≥${MIN_SOCIAL_COUNT}, 持币(>1h:≥${HOLDERS_THRESHOLD_OLD}, ≤1h:≥${HOLDERS_THRESHOLD_YOUNG}), 总量=${TOTAL_SUPPLY.toLocaleString()}, 当前价(>1h:≤${MAX_CURRENT_PRICE_OLD}, ≤1h:≤${MAX_CURRENT_PRICE_YOUNG})`);
  console.log("[SCAN] 同时抓取热点关键词...");
  const [s2, hotspots] = await Promise.all([
    stage2_detail(s1, nowMs),
    fetchAllHotspots(),
  ]);
  console.log(`[SCAN] Stage2 通过: ${s2.length}/${s1.length}`);

  // Stage 3: K线筛
  console.log(`[SCAN] Stage3 K线筛条件: ATH≤$${MAX_HIGH_PRICE}, 前2h最高(币龄>2h时)≤$${MAX_EARLY_HIGH_PRICE}, 当前价/ATH在${PRICE_RATIO_LOW * 100}%~${PRICE_RATIO_HIGH * 100}%(币龄<1h跳过)`);
  const s3 = await stage3_kline(s2, hotspots);
  console.log(`[SCAN] Stage3 通过: ${s3.length}/${s2.length}`);

  // Sort by holders descending
  const filtered = s3.sort((a, b) => b.detail.holders - a.detail.holders);

  const result = {
    scanTime,
    totalTokens: apiTokens.length,
    filteredTokens: filtered.length,
    filterCriteria: "社交≥1 + 持币(>1h:≥60,≤1h:≥30) + 总量10亿 + 价(≤1h:≤0.000004,>1h:≤0.00002) + 最高价≤0.00004(>2h前2h≤0.00002) + 价在最高价10%~80%(币龄<1h跳过)",
    tokens: filtered.map(item => {
      const currentPrice = item.dsCurrentPrice || item.detail.price;
      return {
      address: (item.token.tokenAddress || "").toLowerCase(),
      name: item.token.name || "",
      symbol: item.token.shortName || item.token.symbol || "",
      holders: item.detail.holders,
      created_at: parseInt(item.token.createDate || 0),
      total_supply: item.detail.totalSupply,
      price: currentPrice,
      max_price: item.ath,
      high_2h: item.high2h,
      price_ratio: item.ath > 0 ? +(currentPrice / item.ath).toFixed(4) : 0,
      age_hours: +item.ageHours.toFixed(2),
      social_count: item.detail.socialCount,
      social_links: item.detail.socialLinks,
      hot_news: item.hotNews.isHot,
      hot_score: item.hotNews.score,
      hot_keywords: item.hotNews.matched,
      day1_vol: parseFloat(item.token.day1Vol || 0),
      progress: parseFloat(item.token.progress || 0),
    };}),
  };

  // Write to data/
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  const bjNow = new Date(Date.now() + 8 * 3600 * 1000);
  const scanId = bjNow.toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const scanFile = path.join(DATA_DIR, `${scanId}.json`);
  fs.writeFileSync(scanFile, JSON.stringify(result, null, 2));
  console.log(`[SCAN] Wrote ${scanFile}`);

  // Clean up data files older than 7 days
  const MAX_DATA_AGE_DAYS = 7;
  const cutoffMs = Date.now() - MAX_DATA_AGE_DAYS * 24 * 3600 * 1000;
  const dataFiles = fs.readdirSync(DATA_DIR).filter(f => f.endsWith(".json"));
  let cleaned = 0;
  for (const f of dataFiles) {
    const match = f.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})\.json$/);
    if (!match) continue;
    const [, y, mo, d, h, mi, s] = match;
    const fileDate = new Date(`${y}-${mo}-${d}T${h}:${mi}:${s}Z`);
    if (fileDate.getTime() < cutoffMs) {
      fs.unlinkSync(path.join(DATA_DIR, f));
      cleaned++;
    }
  }
  if (cleaned > 0) console.log(`[SCAN] Cleaned ${cleaned} old data files`);

  const elapsed = ((Date.now() - scanStart) / 1000).toFixed(1);
  console.log(`[SCAN] Done in ${elapsed}s. Total: ${result.totalTokens}, Filtered: ${result.filteredTokens}`);
}

main().catch(e => {
  console.error("[SCAN] Fatal error:", e);
  process.exit(1);
});
