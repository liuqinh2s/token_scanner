/**
 * BSC Token Scanner v2 — 链上发现 + 队列淘汰制
 *
 * 设计:
 *   1. 链上发现: RPC eth_getLogs 查 four.meme TokenCreated 事件 (近15分钟)
 *   2. 入场筛: four.meme detail API — 无社交/总量≠10亿 直接淘汰
 *   3. 淘汰检查: 对队列中代币定期检查, 永久淘汰弃盘币 (持币数: RPC Transfer事件 → four.meme)
 *   4. 精筛: 对存活代币执行 K线/价格/持币数 等条件筛选
 *   5. 钱包分析: BscScan tokentx 查开发者+聪明钱的加仓/减仓行为
 *
 * 流动性数据:
 *   - DEX 流动性 > 0 时使用 DexScreener 数据
 *   - 内盘阶段 (未上 DEX) 使用 four.meme marketCap 作为替代
 *   - 同时记录 raisedAmount (已募集 BNB) 和 marketCap (市值 USD)
 *
 * 淘汰条件 (永久剔除):
 *   - 价格从峰值跌 90%+
 *   - 持币地址从 30+ 跌破 10
 *   - 无社交媒体
 *   - 流动性从 >$1k 跌破 $100
 *   - 进度 < 1% 且币龄 > 2h
 *   - 进度 < 5% 且币龄 > 4h
 *   - 币龄 > 5min 且最高持币数 < 3
 *   - 币龄 > 15min 且最高持币数 < 5
 *   - 币龄 > 1h 且最高持币数 < 10
 *   - 币龄 > 72h
 *
 * 精筛排除 (钱包行为):
 *   - 开发者减仓/清仓
 *   - 开发者撤流动性 (减池子)
 *   - 聪明钱减仓/清仓
 *
 * 精筛加分 (钱包行为):
 *   - 开发者加仓
 *   - 开发者加流动性 (加池子)
 *   - 聪明钱加仓
 *
 * 状态持久化: data/queue.json
 */

const https = require("https");
const http = require("http");
const fs = require("fs");
const path = require("path");

// === Local Config ===
const LOCAL_CONFIG_PATH = path.join(__dirname, "..", "config.local.json");
let localConfig = {};
try {
  if (fs.existsSync(LOCAL_CONFIG_PATH)) {
    localConfig = JSON.parse(fs.readFileSync(LOCAL_CONFIG_PATH, "utf-8"));
    console.log("[CONFIG] 已加载 config.local.json");
  }
} catch (e) {
  console.warn(`[CONFIG] 读取 config.local.json 失败: ${e.message}`);
}

let proxyConfig = null;
if (localConfig.proxy && localConfig.proxy.enabled) {
  proxyConfig = localConfig.proxy;
  console.log(`[PROXY] 已启用代理: ${proxyConfig.host}:${proxyConfig.port}`);
}

// === Constants ===
const MAX_AGE_HOURS = 72;
const SCAN_INTERVAL_MIN = 15;
const TOTAL_SUPPLY = 1_000_000_000;
const MIN_SOCIAL_COUNT = 1;

// 精筛阈值 (与 v1 一致)
const MAX_CURRENT_PRICE_OLD = 0.000023;
const MAX_CURRENT_PRICE_YOUNG = 0.0000045;
const MAX_HIGH_PRICE = 0.00004;
const MAX_EARLY_HIGH_PRICE = 0.00002;
const MAX_EARLY_HIGH_PRICE_RELAXED = 0.000023;
const MAX_CURRENT_PRICE_YOUNG_RELAXED = 0.0000045;
const PRICE_RATIO_LOW = 0.4;
const PRICE_RATIO_HIGH = 0.9;
const HOLDERS_THRESHOLD_OLD = 60;
const HOLDERS_THRESHOLD_YOUNG = 30;

// 淘汰阈值
const ELIM_PRICE_DROP_PCT = 0.90;       // 价格从峰值跌 90%
const ELIM_HOLDERS_FLOOR = 10;          // 持币数跌破 10
const ELIM_HOLDERS_PEAK_MIN = 30;       // 持币数曾达到 30 才触发跌破淘汰
const ELIM_LIQ_FLOOR = 100;             // 流动性跌破 $100
const ELIM_LIQ_PEAK_MIN = 1000;         // 流动性曾达到 $1000 才触发跌破淘汰
const ELIM_PROGRESS_MIN = 0.01;         // 进度 < 1%
const ELIM_PROGRESS_AGE_HOURS = 2;      // 进度<1%淘汰的币龄门槛
const ELIM_PROGRESS_MIN_MID = 0.05;     // 进度 < 5%
const ELIM_PROGRESS_AGE_HOURS_MID = 4;  // 进度<5%淘汰的币龄门槛
const ELIM_EARLY_PEAK_HOLDERS = 5;      // 币龄>15min 最高持币数 < 5 淘汰
const ELIM_EARLY_AGE_MIN = 0.25;        // 15 分钟 = 0.25h
const ELIM_TINY_PEAK_HOLDERS = 3;       // 币龄>5min 最高持币数 < 3 淘汰
const ELIM_TINY_AGE_MIN = 5 / 60;       // 5 分钟
const ELIM_MID_PEAK_HOLDERS = 10;       // 币龄>1h 最高持币数 < 10 淘汰
const ELIM_MID_AGE_HOURS = 1;           // 1 小时

// 已知 DEX Router 地址 (用于识别买入/卖出行为)
const KNOWN_DEX_ROUTERS = new Set([
  "0x10ed43c718714eb63d5aa57b78b54704e256024e", // PancakeSwap V2 Router
  "0x13f4ea83d0bd40e75c8222255bc855a974568dd4", // PancakeSwap V3 Router
  "0x1b81d678ffb9c0263b24a97847620c99d213eb14", // PancakeSwap Universal Router
]);

// 零地址/死地址 (转到这些地址不算卖出, 视为销毁)
const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000";
const DEAD_ADDRESS = "0x000000000000000000000000000000000000dead";
const BURN_ADDRESSES = new Set([ZERO_ADDRESS, DEAD_ADDRESS]);

// API endpoints
const FM_DETAIL = "https://four.meme/meme-api/v1/private/token/get/v2";
const FM_SEARCH = "https://four.meme/meme-api/v1/public/token/search";
const DS_BASE = "https://api.dexscreener.com";
const GT_BASE = "https://api.geckoterminal.com/api/v2";
const BSC_RPC = "https://bsc-rpc.publicnode.com/";
const BSCSCAN_API = "https://api.etherscan.io/v2/api";
const BSCSCAN_API_KEY = localConfig.bscscanApiKey || "";

const FOUR_MEME_CONTRACT = "0x5c952063c7fc8610ffdb798152d69f0b9550762b";
const TOKEN_CREATE_TOPIC = "0x396d5e902b675b032348d3d2e9517ee8f0c4a926603fbc075d3d282ff00cad20";
const ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";

// 已知非聪明钱地址 (交易所/合约/稳定币等, 自动发现时排除)
const KNOWN_EXCLUDE_ADDRESSES = new Set([
  ZERO_ADDRESS, DEAD_ADDRESS,
  "0x10ed43c718714eb63d5aa57b78b54704e256024e", // PancakeSwap V2 Router
  "0x13f4ea83d0bd40e75c8222255bc855a974568dd4", // PancakeSwap V3 Router
  "0x1b81d678ffb9c0263b24a97847620c99d213eb14", // PancakeSwap Universal Router
  "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", // WBNB
  "0x55d398326f99059ff775485246999027b3197955", // USDT
  "0xe9e7cea3dedca5984780bafc599bd69add087d56", // BUSD
  "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", // USDC
  FOUR_MEME_CONTRACT.toLowerCase(),
]);

// 聪明钱自动发现配置
const SMART_MONEY_CACHE_TTL = 3600_000;  // 1小时缓存
const SMART_MONEY_MIN_CROSS_FREQ = localConfig.smartMoneyCrossFreq || 2;
let smartMoneyCache = { ts: 0, addresses: new Set() };

// BscScan 可用性追踪 — 连续失败超过阈值则跳过后续调用
let bscScanConsecFails = 0;
const BSCSCAN_FAIL_THRESHOLD = 5; // 连续 5 次返回空则判定不可用

const FM_HEADERS = {
  "Content-Type": "application/json",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
  Accept: "application/json",
  Origin: "https://four.meme",
  Referer: "https://four.meme/",
};
const DS_HEADERS = { Accept: "application/json", "User-Agent": "Mozilla/5.0" };
const GT_HEADERS = { Accept: "application/json", "User-Agent": "Mozilla/5.0" };

const DATA_DIR = path.join(__dirname, "..", "data");
const QUEUE_FILE = path.join(DATA_DIR, "queue.json");

// === Timestamped Logging ===
function _ts() {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(11, 23).replace("T", " ");
}
const _origLog = console.log.bind(console);
const _origWarn = console.warn.bind(console);
const _origErr = console.error.bind(console);
console.log = (...args) => _origLog(`[${_ts()}]`, ...args);
console.warn = (...args) => _origWarn(`[${_ts()}]`, ...args);
console.error = (...args) => _origErr(`[${_ts()}]`, ...args);

// === HTTP Helpers ===
const fmAgent = new https.Agent({ keepAlive: true, maxSockets: 20 });
const gtAgent = new https.Agent({ keepAlive: true, maxSockets: 5 });

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
      if (socket) { reqOpts.socket = socket; reqOpts.agent = false; }
      else if (options.agent) { reqOpts.agent = options.agent; }
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
    if (proxyConfig && isHttps) {
      const connectReq = http.request({
        host: proxyConfig.host, port: proxyConfig.port,
        method: "CONNECT", path: `${urlObj.hostname}:${urlObj.port || 443}`,
        timeout: requestTimeout,
      });
      connectReq.on("connect", (res, socket) => {
        if (res.statusCode === 200) doRequest(socket);
        else reject(new Error(`Proxy CONNECT failed: ${res.statusCode}`));
      });
      connectReq.on("error", reject);
      connectReq.on("timeout", () => { connectReq.destroy(); reject(new Error("proxy timeout")); });
      connectReq.end();
    } else {
      doRequest(null);
    }
  });
}

function rpcCall(method, params) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ jsonrpc: "2.0", method, params, id: 1 });
    const urlObj = new URL(BSC_RPC);
    function doReq(socket) {
      const req = https.request({
        hostname: urlObj.hostname, path: urlObj.pathname,
        method: "POST", headers: { "Content-Type": "application/json" },
        timeout: 30000,
        ...(socket ? { socket, agent: false } : {}),
      }, (r) => {
        let d = ""; r.on("data", c => d += c);
        r.on("end", () => { try { resolve(JSON.parse(d)); } catch { reject(new Error(d)); } });
      });
      req.on("error", reject);
      req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
      req.write(body);
      req.end();
    }
    if (proxyConfig) {
      const connectReq = http.request({
        host: proxyConfig.host, port: proxyConfig.port,
        method: "CONNECT", path: `${urlObj.hostname}:443`, timeout: 30000,
      });
      connectReq.on("connect", (res, socket) => {
        if (res.statusCode === 200) doReq(socket);
        else reject(new Error("proxy connect failed"));
      });
      connectReq.on("error", reject);
      connectReq.end();
    } else {
      doReq(null);
    }
  });
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

class RateLimiter {
  constructor(rps) { this.interval = Math.ceil(1000 / rps); this.queue = []; this.timer = null; }
  acquire() {
    return new Promise(resolve => {
      this.queue.push(resolve);
      if (!this.timer) this._start();
    });
  }
  _start() {
    this.timer = setInterval(() => {
      if (this.queue.length === 0) { clearInterval(this.timer); this.timer = null; return; }
      this.queue.shift()();
    }, this.interval);
    if (this.queue.length > 0) this.queue.shift()();
  }
}
const fmLimiter = new RateLimiter(5);
const gtLimiter = new RateLimiter(1);
const bscScanLimiter = new RateLimiter(5);  // BscScan 免费 5 req/s
let gtRateDelay = 1000;

// ===================================================================
//  队列状态管理
// ===================================================================
/**
 * 队列中每个代币的结构:
 * {
 *   address: string,
 *   name: string,
 *   symbol: string,
 *   createdAt: number (ms),
 *   addedAt: number (ms),
 *   totalSupply: number,
 *   socialCount: number,
 *   socialLinks: {},
 *   descr: string,
 *   // 动态数据 (每周期更新)
 *   price: number,
 *   peakPrice: number,
 *   holders: number,
 *   peakHolders: number,
 *   liquidity: number,       // USD
 *   peakLiquidity: number,
 *   progress: number,
 *   consecDrops: number,     // 连续价格下跌周期数
 *   lastPrice: number,       // 上周期价格
 *   eliminatedAt: number,    // 淘汰时间 (0=存活)
 *   elimReason: string,
 * }
 */

function loadQueue() {
  try {
    if (fs.existsSync(QUEUE_FILE)) {
      const data = JSON.parse(fs.readFileSync(QUEUE_FILE, "utf-8"));
      console.log(`[QUEUE] 加载队列: ${data.tokens.length} 个代币, lastBlock: ${data.lastBlock}`);
      return data;
    }
  } catch (e) {
    console.warn(`[QUEUE] 加载失败: ${e.message}`);
  }
  return { tokens: [], eliminated: [], lastBlock: 0, lastScanTime: 0 };
}

function saveQueue(queue) {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  // 只保留最近 1000 条淘汰记录
  if (queue.eliminated.length > 1000) {
    queue.eliminated = queue.eliminated.slice(-1000);
  }
  fs.writeFileSync(QUEUE_FILE, JSON.stringify(queue, null, 2));
}

// ===================================================================
//  Step 1: 链上发现 — RPC eth_getLogs 查 TokenCreated 事件
// ===================================================================
async function discoverOnChain(fromBlock) {
  const blockRes = await rpcCall("eth_blockNumber", []);
  const latestBlock = parseInt(blockRes.result, 16);

  if (fromBlock <= 0) {
    // 首次运行: 只扫最近 15 分钟 (~2000 blocks)
    fromBlock = latestBlock - 2000;
  }

  // 安全上限: 不超过 10000 blocks (防止首次运行或长时间中断后扫太多)
  if (latestBlock - fromBlock > 10000) {
    console.warn(`[CHAIN] 区块跨度过大 (${latestBlock - fromBlock}), 截断到最近 10000 blocks`);
    fromBlock = latestBlock - 10000;
  }

  console.log(`[CHAIN] 扫描区块 ${fromBlock} ~ ${latestBlock} (${latestBlock - fromBlock} blocks)`);

  const tokens = [];
  const CHUNK = 10000;
  let current = fromBlock;

  while (current <= latestBlock) {
    const end = Math.min(current + CHUNK - 1, latestBlock);
    try {
      const res = await rpcCall("eth_getLogs", [{
        address: FOUR_MEME_CONTRACT,
        fromBlock: "0x" + current.toString(16),
        toBlock: "0x" + end.toString(16),
        topics: [TOKEN_CREATE_TOPIC],
      }]);

      if (res.error) {
        // 历史裁剪: 跳过
        if (res.error.message && res.error.message.includes("pruned")) {
          current = end + 50000;
          continue;
        }
        console.warn(`[CHAIN] RPC error: ${res.error.message || JSON.stringify(res.error)}`);
        current = end + 1;
        continue;
      }

      for (const log of (res.result || [])) {
        const data = log.data.slice(2);
        const tokenAddr = ("0x" + data.slice(88, 128)).toLowerCase();
        // 只保留 four.meme 代币 (后缀 4444 或 ffff)
        if (!tokenAddr.endsWith("4444") && !tokenAddr.endsWith("ffff")) continue;

        const creatorAddr = ("0x" + data.slice(24, 64)).toLowerCase();
        const createTs = parseInt(data.slice(384, 448), 16); // word[6]

        // 解码名称 (word[9]) 和符号 (word[11] 或 word[12])
        let name = "", symbol = "";
        try {
          const nameLen = parseInt(data.slice(512, 576), 16); // word[8]
          if (nameLen > 0 && nameLen < 200) {
            name = Buffer.from(data.slice(576, 576 + nameLen * 2), "hex").toString("utf8");
          }
          // symbol 位置取决于 name 长度 (动态编码)
          const nameWords = Math.ceil(nameLen / 32) || 1;
          const symLenOffset = (9 + nameWords) * 64; // word after name data
          if (symLenOffset + 64 <= data.length) {
            const symLen = parseInt(data.slice(symLenOffset, symLenOffset + 64), 16);
            if (symLen > 0 && symLen < 100) {
              symbol = Buffer.from(data.slice(symLenOffset + 64, symLenOffset + 64 + symLen * 2), "hex").toString("utf8");
            }
          }
        } catch (e) { /* 解码失败, 后续从 detail API 获取 */ }

        tokens.push({
          address: tokenAddr,
          creator: creatorAddr,
          createdAt: createTs * 1000,
          name,
          symbol,
          block: parseInt(log.blockNumber, 16),
        });
      }
    } catch (e) {
      console.warn(`[CHAIN] 请求失败: ${e.message}`);
      await sleep(1000);
    }
    current = end + 1;
    if (current <= latestBlock) await sleep(50); // 多 chunk 时短暂间隔
  }

  console.log(`[CHAIN] 发现 ${tokens.length} 个新代币`);
  return { tokens, latestBlock };
}

// ===================================================================
//  Step 2: 入场筛 — four.meme detail API
// ===================================================================
async function fetchTokenDetail(tokenAddress) {
  await fmLimiter.acquire();
  try {
    const res = await fetchJSON(`${FM_DETAIL}?address=${tokenAddress}`, {
      headers: FM_HEADERS, agent: fmAgent,
    });
    if (!res.data || !res.data.data) return null;
    const d = res.data.data;
    const tp = d.tokenPrice || {};
    const socialLinks = {};
    if (d.twitterUrl) socialLinks.twitter = d.twitterUrl;
    if (d.telegramUrl) socialLinks.telegram = d.telegramUrl;
    if (d.webUrl) socialLinks.website = d.webUrl;
    // 内盘阶段 DEX 流动性为 0, 用 marketCap 作为替代指标
    const dexLiq = parseFloat(tp.liquidity || 0);
    const marketCap = parseFloat(tp.marketCap || 0);
    const raisedAmount = parseFloat(tp.raisedAmount || 0);
    return {
      holders: parseInt(tp.holderCount || 0, 10),
      price: parseFloat(tp.price || 0),
      totalSupply: parseInt(d.totalAmount || 0, 10),
      socialCount: Object.keys(socialLinks).length,
      socialLinks,
      descr: d.descr || "",
      name: d.name || "",
      shortName: d.shortName || "",
      progress: parseFloat(tp.progress || d.progress || 0),
      day1Vol: parseFloat(tp.day1Vol || d.day1Vol || 0),
      liquidity: dexLiq > 0 ? dexLiq : marketCap,
      raisedAmount,
      marketCap,
    };
  } catch (e) { /* silent */ }
  return null;
}

async function admissionFilter(newTokens, existingAddrs) {
  const admitted = [];
  const rejected = [];
  const CONCURRENCY = 10;  // 提高并发, fmLimiter 控制速率

  // 过滤已在队列或已淘汰的
  const fresh = newTokens.filter(t => !existingAddrs.has(t.address));
  if (fresh.length === 0) return { admitted, rejected };

  console.log(`[入场] 对 ${fresh.length} 个新代币调 detail API...`);

  for (let i = 0; i < fresh.length; i += CONCURRENCY) {
    const batch = fresh.slice(i, i + CONCURRENCY);
    const results = await Promise.all(batch.map(async (t) => {
      const detail = await fetchTokenDetail(t.address);
      if (!detail) return { token: t, detail: null, reason: "API无数据" };
      // 入场条件: 社交 ≥ 1, 总供应量 = 10亿
      if (detail.socialCount < MIN_SOCIAL_COUNT) return { token: t, detail, reason: "无社交媒体" };
      if (detail.totalSupply !== TOTAL_SUPPLY) return { token: t, detail, reason: `总量${detail.totalSupply}≠10亿` };
      return { token: t, detail, reason: null };
    }));

    for (const r of results) {
      if (r.reason) {
        rejected.push(r);
      } else {
        admitted.push(r);
      }
    }
  }

  console.log(`[入场] 通过: ${admitted.length}/${fresh.length} (淘汰 ${rejected.length}: 无社交/总量不符)`);
  return { admitted, rejected };
}

// ===================================================================
//  Step 3: 淘汰检查 — DexScreener + four.meme detail
// ===================================================================

/** BscScan 批量查持币地址数 (Etherscan V2 API, chainid=56) */
async function bscScanHolderCounts(addresses) {
  const result = new Map(); // addr -> holderCount
  if (!BSCSCAN_API_KEY) return result;

  // 短路: BscScan 连续返回空, 跳过无效请求
  if (bscScanConsecFails >= BSCSCAN_FAIL_THRESHOLD) {
    console.log(`[BSCSCAN] 跳过持币数查询 (连续 ${bscScanConsecFails} 次无数据, 使用 four.meme 数据)`);
    return result;
  }

  // 先用少量地址探测可用性 (最多 3 个)
  const probeAddrs = addresses.slice(0, Math.min(3, addresses.length));
  let probeHits = 0;
  for (const addr of probeAddrs) {
    await bscScanLimiter.acquire();
    try {
      const url = `${BSCSCAN_API}?chainid=56&module=token&action=tokenholdercount&contractaddress=${addr}&apikey=${BSCSCAN_API_KEY}`;
      const res = await fetchJSON(url, { timeout: 8000 });
      if (res.data && res.data.status === "1" && res.data.result) {
        result.set(addr, parseInt(res.data.result, 10));
        probeHits++;
      }
    } catch (e) { /* silent */ }
  }

  // 探测全部失败 → 累计失败计数, 跳过剩余
  if (probeHits === 0) {
    bscScanConsecFails += probeAddrs.length;
    console.log(`[BSCSCAN] 查到 0/${addresses.length} 个代币持币数 (探测无数据, 跳过剩余)`);
    return result;
  }

  // 探测有数据 → 重置失败计数, 继续查剩余
  bscScanConsecFails = 0;
  const remaining = addresses.slice(probeAddrs.length);
  for (const addr of remaining) {
    await bscScanLimiter.acquire();
    try {
      const url = `${BSCSCAN_API}?chainid=56&module=token&action=tokenholdercount&contractaddress=${addr}&apikey=${BSCSCAN_API_KEY}`;
      const res = await fetchJSON(url, { timeout: 8000 });
      if (res.data && res.data.status === "1" && res.data.result) {
        result.set(addr, parseInt(res.data.result, 10));
      }
    } catch (e) { /* silent */ }
  }
  console.log(`[BSCSCAN] 查到 ${result.size}/${addresses.length} 个代币持币数`);
  return result;
}

/**
 * RPC 查持币地址数 — 通过 eth_getLogs 查 ERC-20 Transfer 事件
 * 统计所有接收过代币的唯一地址 (排除零地址/销毁地址)
 * 作为 BscScan tokenholdercount (PRO 端点) 的免费替代方案
 * 注意: 这是近似值, 不排除余额为0的地址, 但对新币足够准确
 */
async function rpcHolderCounts(tokenInfos) {
  // tokenInfos: [{ address, block, createdAt }] — block 是代币创建时的区块号
  const result = new Map();
  const CONCURRENCY = 5;

  // 获取当前区块号, 用于估算缺失 block 的代币
  let latestBlock = 0;
  try {
    const blockRes = await rpcCall("eth_blockNumber", []);
    latestBlock = parseInt(blockRes.result, 16);
  } catch (e) { /* 静默 */ }

  for (let i = 0; i < tokenInfos.length; i += CONCURRENCY) {
    const batch = tokenInfos.slice(i, i + CONCURRENCY);
    const promises = batch.map(async ({ address, block, createdAt }) => {
      try {
        // 从创建区块开始查, 避免全链扫描被 RPC 拒绝
        let fromBlock;
        if (block > 0) {
          fromBlock = "0x" + Math.max(0, block - 1).toString(16);
        } else if (createdAt > 0 && latestBlock > 0) {
          // 用创建时间估算区块号 (BSC ~3秒/块)
          const ageSec = Math.max(0, (Date.now() - createdAt) / 1000);
          const estBlock = Math.max(0, latestBlock - Math.ceil(ageSec / 3) - 100);
          fromBlock = "0x" + estBlock.toString(16);
        } else {
          // 无法确定起始区块, 只查最近 50000 块 (~42小时)
          const fallback = Math.max(0, latestBlock - 50000);
          fromBlock = "0x" + fallback.toString(16);
        }
        const res = await Promise.race([
          rpcCall("eth_getLogs", [{
            address,
            fromBlock,
            toBlock: "latest",
            topics: [ERC20_TRANSFER_TOPIC],
          }]),
          sleep(10000).then(() => ({ error: { message: "timeout" } })),
        ]);
        if (res.error) return;
        const logs = res.result || [];
        const holders = new Set();
        for (const log of logs) {
          if (!log.topics || log.topics.length < 3) continue;
          const to = ("0x" + log.topics[2].slice(26)).toLowerCase();
          if (!BURN_ADDRESSES.has(to)) holders.add(to);
        }
        holders.delete(ZERO_ADDRESS);
        holders.delete(DEAD_ADDRESS);
        holders.delete(address.toLowerCase());
        holders.delete(FOUR_MEME_CONTRACT.toLowerCase());
        if (holders.size > 0) {
          result.set(address, holders.size);
        }
      } catch (e) {
        // 静默失败
      }
    });
    await Promise.all(promises);
    if (i + CONCURRENCY < tokenInfos.length) await sleep(200);
  }
  console.log(`[RPC] 查到 ${result.size}/${tokenInfos.length} 个代币持币数`);
  return result;
}

/** DexScreener 批量查价格+流动性 (最多 30 个地址/请求) */
async function dsBatchPrices(addresses) {
  const result = new Map(); // addr -> { price, liquidity }
  const BATCH = 30;

  for (let i = 0; i < addresses.length; i += BATCH) {
    const batch = addresses.slice(i, i + BATCH);
    try {
      const res = await fetchJSON(`${DS_BASE}/tokens/v1/bsc/${batch.join(",")}`, {
        headers: DS_HEADERS, timeout: 15000,
      });
      if (res.status === 429) {
        await sleep(2000);
        continue;
      }
      const pairs = Array.isArray(res.data) ? res.data : (res.data?.pairs || []);
      for (const p of pairs) {
        if (!p.baseToken) continue;
        const addr = p.baseToken.address.toLowerCase();
        if (result.has(addr)) continue;
        result.set(addr, {
          price: parseFloat(p.priceUsd || 0),
          liquidity: parseFloat(p.liquidity?.usd || 0),
          volume24h: parseFloat(p.volume?.h24 || 0),
          name: p.baseToken.name || "",
          symbol: p.baseToken.symbol || "",
        });
      }
    } catch (e) {
      console.warn(`[DS] 批量查价失败: ${e.message}`);
    }
    if (i + BATCH < addresses.length) await sleep(300);
  }
  return result;
}

/** 淘汰检查: 返回 { survivors: [], eliminated: [] } */
async function eliminationCheck(queue, nowMs) {
  const survivors = [];
  const eliminated = [];

  if (queue.length === 0) return { survivors, eliminated };

  // 1. 币龄淘汰 (无需 API)
  const maxAgeMs = MAX_AGE_HOURS * 3600 * 1000;
  const ageFiltered = [];
  for (const t of queue) {
    if (nowMs - t.createdAt > maxAgeMs) {
      eliminated.push({ ...t, eliminatedAt: nowMs, elimReason: `币龄>${MAX_AGE_HOURS}h` });
    } else {
      ageFiltered.push(t);
    }
  }
  if (eliminated.length > 0) {
    console.log(`[淘汰] 币龄超限: ${eliminated.length} 个`);
  }

  if (ageFiltered.length === 0) return { survivors, eliminated };

  // 2. DexScreener 批量查价格+流动性 与 RPC 持币数 并行
  const addrs = ageFiltered.map(t => t.address);
  const tokenInfosForRpc = ageFiltered.map(t => ({ address: t.address, block: t.block || 0, createdAt: t.createdAt || 0 }));
  const [dsData, rpcHolders] = await Promise.all([
    dsBatchPrices(addrs),
    rpcHolderCounts(tokenInfosForRpc),
  ]);

  // 3. four.meme detail 查社交/进度 (并发)
  console.log(`[淘汰] 查询 ${ageFiltered.length} 个代币详情...`);
  const CONCURRENCY = 10;  // 提高并发, fmLimiter 控制速率
  const detailMap = new Map();
  for (let i = 0; i < ageFiltered.length; i += CONCURRENCY) {
    const batch = ageFiltered.slice(i, i + CONCURRENCY);
    const results = await Promise.all(batch.map(async (t) => {
      const d = await fetchTokenDetail(t.address);
      return { address: t.address, detail: d };
    }));
    for (const r of results) {
      if (r.detail) detailMap.set(r.address, r.detail);
    }
  }

  // 4. 逐个检查淘汰条件
  for (const t of ageFiltered) {
    const ds = dsData.get(t.address);
    const detail = detailMap.get(t.address);
    const ageHours = (nowMs - t.createdAt) / 3600000;

    // 更新动态数据
    const currentPrice = ds?.price || detail?.price || t.price || 0;
    const rpcHolder = rpcHolders.get(t.address);
    const currentHolders = rpcHolder != null ? rpcHolder : (detail?.holders || t.holders || 0);
    const currentLiq = ds?.liquidity || detail?.liquidity || t.liquidity || 0;
    const currentProgress = detail?.progress || t.progress || 0;

    t.price = currentPrice;
    t.holders = currentHolders;
    t.liquidity = currentLiq;
    t.progress = currentProgress;
    if (detail) {
      t.socialCount = detail.socialCount;
      t.socialLinks = detail.socialLinks;
      t.day1Vol = detail.day1Vol || t.day1Vol || 0;
      t.raisedAmount = detail.raisedAmount || t.raisedAmount || 0;
      t.marketCap = detail.marketCap || t.marketCap || 0;
    }
    if (ds) {
      t.name = ds.name || t.name;
      t.symbol = ds.symbol || t.symbol;
    }

    // 更新峰值
    t.peakPrice = Math.max(t.peakPrice || 0, currentPrice);
    t.peakHolders = Math.max(t.peakHolders || 0, currentHolders);
    t.peakLiquidity = Math.max(t.peakLiquidity || 0, currentLiq);

    // 连续下跌计数
    if (t.lastPrice > 0 && currentPrice < t.lastPrice) {
      t.consecDrops = (t.consecDrops || 0) + 1;
    } else {
      t.consecDrops = 0;
    }
    t.lastPrice = currentPrice;

    // --- 淘汰条件 ---
    let elimReason = null;

    // 1. 价格从峰值跌 90%+
    if (t.peakPrice > 0 && currentPrice > 0 && currentPrice < t.peakPrice * (1 - ELIM_PRICE_DROP_PCT)) {
      elimReason = `价格跌${((1 - currentPrice / t.peakPrice) * 100).toFixed(0)}% (峰:${t.peakPrice.toExponential(2)} 现:${currentPrice.toExponential(2)})`;
    }
    // 2. 持币数从 30+ 跌破 10
    if (!elimReason && t.peakHolders >= ELIM_HOLDERS_PEAK_MIN && currentHolders < ELIM_HOLDERS_FLOOR) {
      elimReason = `持币数 ${t.peakHolders}→${currentHolders}`;
    }
    // 3. 无社交媒体 (可能创建时有, 后来删了)
    if (!elimReason && detail && detail.socialCount < MIN_SOCIAL_COUNT) {
      elimReason = "无社交媒体";
    }
    // 4. 流动性从 >$1k 跌破 $100
    if (!elimReason && t.peakLiquidity >= ELIM_LIQ_PEAK_MIN && currentLiq < ELIM_LIQ_FLOOR) {
      elimReason = `流动性 $${t.peakLiquidity.toFixed(0)}→$${currentLiq.toFixed(0)}`;
    }
    // 5. 进度 < 1% 且币龄 > 2h
    if (!elimReason && ageHours > ELIM_PROGRESS_AGE_HOURS && currentProgress < ELIM_PROGRESS_MIN) {
      elimReason = `进度${(currentProgress * 100).toFixed(2)}% 币龄${ageHours.toFixed(1)}h`;
    }
    // 5b. 进度 < 5% 且币龄 > 4h
    if (!elimReason && ageHours > ELIM_PROGRESS_AGE_HOURS_MID && currentProgress < ELIM_PROGRESS_MIN_MID) {
      elimReason = `进度${(currentProgress * 100).toFixed(2)}% 币龄${ageHours.toFixed(1)}h`;
    }
    // 6. 币龄>5min 最高持币数 < 3
    if (!elimReason && ageHours > ELIM_TINY_AGE_MIN && t.peakHolders < ELIM_TINY_PEAK_HOLDERS) {
      elimReason = `币龄${ageHours.toFixed(1)}h 最高持币仅${t.peakHolders}`;
    }
    // 7. 币龄>15min 最高持币数 < 5
    if (!elimReason && ageHours > ELIM_EARLY_AGE_MIN && t.peakHolders < ELIM_EARLY_PEAK_HOLDERS) {
      elimReason = `币龄${ageHours.toFixed(1)}h 最高持币仅${t.peakHolders}`;
    }
    // 8. 币龄>1h 最高持币数 < 10
    if (!elimReason && ageHours > ELIM_MID_AGE_HOURS && t.peakHolders < ELIM_MID_PEAK_HOLDERS) {
      elimReason = `币龄${ageHours.toFixed(1)}h 最高持币仅${t.peakHolders}`;
    }

    if (elimReason) {
      eliminated.push({ ...t, eliminatedAt: nowMs, elimReason });
    } else {
      survivors.push(t);
    }
  }

  const elimCount = eliminated.length - (queue.length - ageFiltered.length); // 不含币龄淘汰
  if (elimCount > 0) {
    console.log(`[淘汰] 条件淘汰: ${elimCount} 个`);
    for (const e of eliminated.slice(-elimCount)) {
      console.log(`  ✗ ${e.name || e.address.slice(0, 16)} — ${e.elimReason}`);
    }
  }

  return { survivors, eliminated };
}

// ===================================================================
//  Step 4.5: 钱包行为分析 — BscScan tokentx 查开发者+聪明钱
//  参考: github.com/liuqinh2s/token_trading 的链上行为分析方案
//  改进:
//    - 聪明钱自动发现 (Top Holders 交叉分析)
//    - LP token mint/burn 检测流动性操作
//    - 销毁地址排除 (转到零地址/死地址不算卖出)
//    - DEX Router 精确匹配买卖方向
// ===================================================================

/**
 * BscScan 统一 GET 请求封装 (带重试和 429 处理)
 */
async function bscScanGet(params) {
  if (!BSCSCAN_API_KEY) return null;
  const MAX_RETRIES = 2;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    await bscScanLimiter.acquire();
    try {
      const qs = new URLSearchParams({ ...params, apikey: BSCSCAN_API_KEY, chainid: "56" }).toString();
      const res = await fetchJSON(`${BSCSCAN_API}?${qs}`, { timeout: 15000 });
      if (res.status === 429) {
        const wait = 3000 * (attempt + 1);
        console.warn(`[BSCSCAN] 429, 等待 ${wait / 1000}s (${attempt + 1}/${MAX_RETRIES + 1})`);
        await sleep(wait);
        continue;
      }
      return res.data || null;
    } catch (e) {
      if (attempt < MAX_RETRIES) await sleep(2000);
    }
  }
  return null;
}

/**
 * BscScan 查指定地址对某代币的转账记录
 */
async function bscScanTokenTxByAddress(tokenAddress, address) {
  const d = await bscScanGet({
    module: "account", action: "tokentx",
    contractaddress: tokenAddress, address,
    page: 1, offset: 100, sort: "desc",
  });
  if (d && d.status === "1" && Array.isArray(d.result)) return d.result;
  return [];
}

/**
 * BscScan 查代币全量转账记录 (用于聪明钱分析)
 */
async function bscScanTokenTxAll(tokenAddress) {
  const d = await bscScanGet({
    module: "account", action: "tokentx",
    contractaddress: tokenAddress,
    page: 1, offset: 200, sort: "desc",
  });
  if (d && d.status === "1" && Array.isArray(d.result)) return d.result;
  return [];
}

/**
 * BscScan 查 Top Holders 列表
 */
async function bscScanTopHolders(tokenAddress, offset = 50) {
  const d = await bscScanGet({
    module: "token", action: "tokenholderlist",
    contractaddress: tokenAddress,
    page: 1, offset,
  });
  if (d && d.status === "1" && Array.isArray(d.result)) return d.result;
  return [];
}

// ===================================================================
//  聪明钱自动发现 — Top Holders 交叉分析
// ===================================================================

/**
 * 从 GeckoTerminal BSC trending pools 获取近期涨幅大的已上 DEX 代币
 * 替代 four.meme HOT 列表 — 已上 DEX 的代币有真实交易, Top Holders 交叉分析更有效
 */
async function fetchSmartMoneySourceTokens(limit = 10) {
  try {
    const res = await gtRequest(`${GT_BASE}/networks/bsc/trending_pools`);
    if (!res || !res.data) return [];
    const pools = res.data;
    // 筛选: 24h 涨幅 > 10% 且有足够交易量的池子
    const seen = new Set();
    const tokens = [];
    for (const pool of pools) {
      const attrs = pool.attributes || {};
      const h24Change = parseFloat(attrs.price_change_percentage?.h24 || 0);
      const h24Vol = parseFloat(attrs.volume_usd?.h24 || 0);
      if (h24Change < 10 || h24Vol < 50000) continue;
      // 提取 base token 地址
      const baseTokenId = pool.relationships?.base_token?.data?.id || "";
      const addr = baseTokenId.replace("bsc_", "").toLowerCase();
      if (!addr || addr.length !== 42 || seen.has(addr)) continue;
      // 排除稳定币和 WBNB
      if (KNOWN_EXCLUDE_ADDRESSES.has(addr)) continue;
      seen.add(addr);
      tokens.push(addr);
      if (tokens.length >= limit) break;
    }
    if (tokens.length > 0) {
      console.log(`[聪明钱] GeckoTerminal trending: ${tokens.length} 个涨幅代币`);
    }
    return tokens;
  } catch (e) {
    console.warn(`[聪明钱] GeckoTerminal trending 获取失败: ${e.message}`);
    return [];
  }
}

/**
 * RPC 查 Top Holders — 通过 Transfer 事件统计净持仓, 取 Top N
 * 替代 BscScan tokenholderlist (PRO 端点)
 */
async function rpcTopHolders(tokenAddress, topN = 50, fromBlock = 0) {
  try {
    let fb;
    if (fromBlock > 0) {
      fb = "0x" + Math.max(0, fromBlock - 1).toString(16);
    } else {
      // 查最近 100000 块 (~3.5天)
      const blockRes = await rpcCall("eth_blockNumber", []);
      const latest = parseInt(blockRes.result, 16);
      fb = "0x" + Math.max(0, latest - 100000).toString(16);
    }
    const res = await Promise.race([
      rpcCall("eth_getLogs", [{
        address: tokenAddress,
        fromBlock: fb,
        toBlock: "latest",
        topics: [ERC20_TRANSFER_TOPIC],
      }]),
      sleep(15000).then(() => ({ error: { message: "timeout" } })),
    ]);
    if (res.error) return [];
    const logs = res.result || [];
    const balances = new Map(); // addr -> net balance (简化: 用转入-转出次数近似)
    for (const log of logs) {
      if (!log.topics || log.topics.length < 3) continue;
      const from = ("0x" + log.topics[1].slice(26)).toLowerCase();
      const to = ("0x" + log.topics[2].slice(26)).toLowerCase();
      // 用转账金额更准确
      const value = log.data ? BigInt(log.data) : 0n;
      if (!BURN_ADDRESSES.has(from) && !KNOWN_EXCLUDE_ADDRESSES.has(from)) {
        balances.set(from, (balances.get(from) || 0n) - value);
      }
      if (!BURN_ADDRESSES.has(to) && !KNOWN_EXCLUDE_ADDRESSES.has(to)) {
        balances.set(to, (balances.get(to) || 0n) + value);
      }
    }
    // 排序取 Top N (余额 > 0 的)
    return [...balances.entries()]
      .filter(([, bal]) => bal > 0n)
      .sort((a, b) => (b[1] > a[1] ? 1 : b[1] < a[1] ? -1 : 0))
      .slice(0, topN)
      .map(([addr]) => addr);
  } catch (e) {
    return [];
  }
}

/**
 * 聪明钱自动发现: Top Holders 交叉分析
 * 逻辑: 获取多个热门代币的 Top 50 Holders, 在 ≥2 个代币中都是大户的地址 → 聪明钱
 * 数据源: RPC Transfer 事件 (替代 BscScan PRO 端点)
 */
async function discoverSmartMoneyFromTopHolders() {
  const hotTokens = await fetchSmartMoneySourceTokens(10);
  if (hotTokens.length === 0) return new Set();

  console.log(`[聪明钱] 分析 ${hotTokens.length} 个热门代币的 Top Holders (RPC)...`);
  const addrFreq = new Map();

  const CONCURRENCY = 3;
  for (let i = 0; i < hotTokens.length; i += CONCURRENCY) {
    const batch = hotTokens.slice(i, i + CONCURRENCY);
    const results = await Promise.all(batch.map(addr => rpcTopHolders(addr, 50)));
    for (const holders of results) {
      for (const addr of holders) {
        addrFreq.set(addr, (addrFreq.get(addr) || 0) + 1);
      }
    }
    if (i + CONCURRENCY < hotTokens.length) await sleep(200);
  }

  const discovered = new Set();
  for (const [addr, freq] of addrFreq) {
    if (freq >= SMART_MONEY_MIN_CROSS_FREQ) {
      discovered.add(addr);
    }
  }

  if (discovered.size > 0) {
    console.log(`[聪明钱] Top Holders 交叉分析发现 ${discovered.size} 个地址 (出现≥${SMART_MONEY_MIN_CROSS_FREQ}次)`);
  }
  return discovered;
}

/**
 * 加载聪明钱地址 (带缓存, 多源合并)
 * 来源: 1. config.local.json 手动配置  2. Top Holders 交叉分析自动发现
 */
async function loadSmartMoneyAddresses() {
  const now = Date.now();
  if (now - smartMoneyCache.ts < SMART_MONEY_CACHE_TTL && smartMoneyCache.addresses.size > 0) {
    return smartMoneyCache.addresses;
  }

  const addresses = new Set();

  // 来源 1: 手动配置
  for (const addr of (localConfig.smartMoneyAddrs || [])) {
    const a = (addr || "").trim().toLowerCase();
    if (a && a.length === 42 && !KNOWN_EXCLUDE_ADDRESSES.has(a)) {
      addresses.add(a);
    }
  }
  const manualCount = addresses.size;

  // 来源 2: Top Holders 交叉分析自动发现
  const discovered = await discoverSmartMoneyFromTopHolders();
  for (const a of discovered) addresses.add(a);

  // 排除已知非聪明钱地址
  for (const ex of KNOWN_EXCLUDE_ADDRESSES) addresses.delete(ex);

  console.log(`[聪明钱] 地址总计: ${addresses.size} 个 (手动 ${manualCount}, 自动发现 ${discovered.size})`);
  smartMoneyCache = { ts: now, addresses };
  return addresses;
}

// ===================================================================
//  开发者行为分析
// ===================================================================

/**
 * 分析开发者链上行为 (参考 token_trading 的 analyze_developer_behavior)
 * 改进: DEX Router 精确匹配, LP token mint/burn 检测, 销毁地址排除
 */
async function analyzeDeveloperBehavior(tokenAddress, creatorAddress) {
  const result = {
    hasSell: false, hasBuy: false,
    hasLpAdd: false, hasLpRemove: false,
    sellPct: 0, details: [],
    bonus: 0, exclude: false,
  };
  if (!creatorAddress || !BSCSCAN_API_KEY) return result;

  const tokenLower = tokenAddress.toLowerCase();
  const creatorLower = creatorAddress.toLowerCase();

  // 查开发者对该代币的转账记录
  const transfers = await bscScanTokenTxByAddress(tokenAddress, creatorAddress);
  if (transfers.length === 0) return result;

  let totalIn = 0, totalOut = 0;
  let buyCount = 0, sellCount = 0;
  let lpAddCount = 0, lpRemoveCount = 0;

  for (const tx of transfers) {
    const from = (tx.from || "").toLowerCase();
    const to = (tx.to || "").toLowerCase();
    const value = parseInt(tx.value || "0", 10);
    const contractAddr = (tx.contractAddress || "").toLowerCase();
    if (value <= 0) continue;

    // 该代币的转账
    if (contractAddr === tokenLower) {
      if (to === creatorLower) {
        // 开发者收到代币
        totalIn += value;
        // 从 DEX Router 或零地址收到 = 买入
        if (KNOWN_DEX_ROUTERS.has(from) || from === ZERO_ADDRESS) {
          buyCount++;
        }
      } else if (from === creatorLower) {
        // 开发者转出代币
        // 转到零地址/死地址 = 销毁, 不算卖出
        if (BURN_ADDRESSES.has(to)) continue;
        totalOut += value;
        // 转到 DEX Router = 卖出
        if (KNOWN_DEX_ROUTERS.has(to)) {
          sellCount++;
        } else {
          // 转到其他地址也算减仓
          sellCount++;
        }
      }
    } else {
      // 非该代币的转账 — 检查是否是 LP token 操作
      // LP token 的 from=0x0 → 加流动性 (mint), to=0x0 → 撤流动性 (burn)
      const tokenName = (tx.tokenName || "").toLowerCase();
      if (tokenName.includes("lp") || tokenName.includes("pancake")) {
        if (from === ZERO_ADDRESS && to === creatorLower) {
          lpAddCount++;
        } else if (from === creatorLower && to === ZERO_ADDRESS) {
          lpRemoveCount++;
        }
      }
    }
  }

  // 计算卖出占比
  if (totalIn > 0) {
    result.sellPct = Math.min(100, (totalOut / totalIn) * 100);
  }

  // 判断行为
  if (sellCount > 0) {
    result.hasSell = true;
    if (result.sellPct >= 90) {
      result.details.push(`开发者清仓 (卖出${result.sellPct.toFixed(0)}%)`);
    } else {
      result.details.push(`开发者减仓 (卖出${result.sellPct.toFixed(0)}%)`);
    }
    result.exclude = true;
  }
  if (buyCount > 0) {
    result.hasBuy = true;
    result.details.push(`开发者加仓 (${buyCount}笔)`);
    result.bonus++;
  }
  if (lpAddCount > 0) {
    result.hasLpAdd = true;
    result.details.push(`开发者加池子 (${lpAddCount}笔)`);
    result.bonus++;
  }
  if (lpRemoveCount > 0) {
    result.hasLpRemove = true;
    result.details.push(`开发者撤池子 (${lpRemoveCount}笔)`);
    result.exclude = true;
  }

  return result;
}

// ===================================================================
//  聪明钱行为分析
// ===================================================================

/**
 * 分析聪明钱对该代币的链上行为 (参考 token_trading 的 analyze_smart_money_behavior)
 * 改进: DEX Router 精确匹配买卖方向, 按地址数计数
 */
async function analyzeSmartMoneyBehavior(tokenAddress, smartAddresses) {
  const result = {
    hasBuy: false, hasSell: false,
    buyCount: 0, sellCount: 0,
    details: [], bonus: 0, exclude: false,
  };
  if (!smartAddresses || smartAddresses.size === 0 || !BSCSCAN_API_KEY) return result;

  // 查该代币的全量转账记录
  const allTransfers = await bscScanTokenTxAll(tokenAddress);
  if (allTransfers.length === 0) return result;

  const buyers = new Set();
  const sellers = new Set();

  for (const tx of allTransfers) {
    const from = (tx.from || "").toLowerCase();
    const to = (tx.to || "").toLowerCase();
    const value = parseInt(tx.value || "0", 10);
    if (value <= 0) continue;

    // 聪明钱买入: 聪明钱是接收方, 来源是 DEX Router 或零地址
    if (smartAddresses.has(to)) {
      if (KNOWN_DEX_ROUTERS.has(from) || from === ZERO_ADDRESS) {
        buyers.add(to);
      }
    }
    // 聪明钱卖出: 聪明钱是发送方, 目标是 DEX Router
    if (smartAddresses.has(from)) {
      if (KNOWN_DEX_ROUTERS.has(to)) {
        sellers.add(from);
      }
    }
  }

  result.buyCount = buyers.size;
  result.sellCount = sellers.size;

  if (buyers.size > 0) {
    result.hasBuy = true;
    result.details.push(`聪明钱加仓 (${buyers.size}个地址)`);
    result.bonus += buyers.size; // 每个聪明钱加仓 +1 分
  }
  if (sellers.size > 0) {
    result.hasSell = true;
    result.details.push(`聪明钱减仓 (${sellers.size}个地址)`);
    result.exclude = true;
  }

  return result;
}

// ===================================================================
//  钱包分析入口 — 合并开发者+聪明钱
// ===================================================================

/**
 * 批量分析钱包行为 (带并发控制)
 * 返回 Map<address, { excluded, excludeReason, signals, bonus, details }>
 */
async function batchWalletAnalysis(tokens, smartAddresses) {
  const CONCURRENCY = 4; // 提高并发, bscScanLimiter 控制速率
  const resultMap = new Map();

  for (let i = 0; i < tokens.length; i += CONCURRENCY) {
    const batch = tokens.slice(i, i + CONCURRENCY);
    const results = await Promise.all(batch.map(async (t) => {
      // 分析开发者行为
      const dev = await analyzeDeveloperBehavior(t.address, t.creator);

      // 分析聪明钱行为
      const sm = await analyzeSmartMoneyBehavior(t.address, smartAddresses);

      // 合并结果
      const allDetails = [...dev.details, ...sm.details];
      const totalBonus = dev.bonus + sm.bonus;
      const signals = [];
      if (dev.hasBuy) signals.push("开发者加仓");
      if (dev.hasLpAdd) signals.push("开发者加池子");
      if (sm.hasBuy) signals.push("聪明钱加仓");

      let excluded = false, excludeReason = "";
      if (dev.exclude) {
        excluded = true;
        excludeReason = dev.details.join(", ");
      }
      if (sm.exclude) {
        excluded = true;
        excludeReason = (excludeReason ? excludeReason + ", " : "") + sm.details.join(", ");
      }

      return {
        address: t.address,
        wa: { excluded, excludeReason, signals, bonus: totalBonus, details: allDetails },
      };
    }));
    for (const r of results) {
      resultMap.set(r.address, r.wa);
    }
  }

  return resultMap;
}

// ===================================================================
//  Step 4: 精筛 — K线 + 价格比 (与 v1 Stage3 一致)
// ===================================================================

async function gtRequest(url, maxRetries = 3) {
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      await gtLimiter.acquire();
      const res = await fetchJSON(url, { headers: GT_HEADERS, agent: gtAgent, timeout: 15000 });
      if (res.status === 429) {
        const wait = 5000 * (attempt + 1);
        gtRateDelay = Math.min(5000, gtRateDelay + 1000);
        await sleep(wait);
        continue;
      }
      gtRateDelay = Math.max(500, gtRateDelay - 200);
      return res.data;
    } catch (e) {
      if (attempt < maxRetries - 1) await sleep(3000);
    }
  }
  return null;
}

async function gtOhlcvDirect(tokenAddress, limit = 72) {
  const url = `${GT_BASE}/networks/bsc/pools/${tokenAddress}/ohlcv/hour?aggregate=1&limit=${limit}`;
  const data = await gtRequest(url);
  if (!data) return [];
  return ((data.data || {}).attributes || {}).ohlcv_list || [];
}

function calcAllTimeHigh(candles) {
  if (!candles || candles.length === 0) return null;
  return Math.max(...candles.map(c => parseFloat(c[2])));
}

function calcMaxPriceFirstNHours(candles, createTsSec, hours = 2) {
  if (!candles || candles.length === 0) return null;
  const cutoff = createTsSec + hours * 3600;
  let maxHigh = 0, found = false;
  for (const c of candles) {
    const ts = parseInt(c[0]);
    if (ts > cutoff || ts < createTsSec - 3600) continue;
    const high = parseFloat(c[2]);
    if (high > maxHigh) maxHigh = high;
    found = true;
  }
  return found ? maxHigh : null;
}

function calcMinPriceExcludeFirst(candles, createTsSec) {
  if (!candles || candles.length < 2) return null;
  const sorted = candles.slice().sort((a, b) => parseInt(a[0]) - parseInt(b[0]));
  const firstTs = parseInt(sorted[0][0]);
  let minLow = Infinity, found = false;
  for (const c of sorted) {
    if (parseInt(c[0]) === firstTs) continue;
    const low = parseFloat(c[3]);
    if (low > 0 && low < minLow) { minLow = low; found = true; }
  }
  return found ? minLow : null;
}

function calcMinPriceAll(candles) {
  if (!candles || candles.length < 1) return null;
  let minLow = Infinity, found = false;
  for (const c of candles) {
    const low = parseFloat(c[3]);
    if (low > 0 && low < minLow) { minLow = low; found = true; }
  }
  return found ? minLow : null;
}

/** 精筛: 对存活代币执行 K线条件筛选 + 钱包行为排除/加分 */
async function qualityFilter(candidates, nowMs, walletMap) {
  const results = [];

  for (let i = 0; i < candidates.length; i++) {
    const t = candidates[i];
    const ageHours = (nowMs - t.createdAt) / 3600000;
    const createTsSec = t.createdAt / 1000;
    const currentPrice = t.price || 0;

    // 钱包行为排除检查
    const wa = walletMap ? walletMap.get(t.address) : null;
    if (wa && wa.excluded) {
      console.log(`[精筛] ✗ ${t.name || t.address.slice(0, 16)} — 钱包排除: ${wa.excludeReason}`);
      continue;
    }

    // 价格初筛
    const isRelaxed = ageHours < 4 && currentPrice > 0.00001;
    const youngLimit = isRelaxed ? MAX_CURRENT_PRICE_YOUNG_RELAXED : MAX_CURRENT_PRICE_YOUNG;
    const maxPrice = ageHours > 1 ? MAX_CURRENT_PRICE_OLD : youngLimit;
    if (currentPrice > maxPrice) continue;

    // 持币数
    if (ageHours > 1 && t.holders < HOLDERS_THRESHOLD_OLD) continue;
    if (ageHours <= 1 && t.holders < HOLDERS_THRESHOLD_YOUNG) continue;

    // K线 (GeckoTerminal) — gtLimiter 已控制速率, 无需额外 sleep
    const candles = await gtOhlcvDirect(t.address, 72);

    let ath = null, high2h = null;
    if (candles && candles.length > 0) {
      high2h = calcMaxPriceFirstNHours(candles, createTsSec, 2);
      ath = calcAllTimeHigh(candles);
    }

    // 用 K线 ATH 修正队列中的 peakPrice (解决15分钟快照遗漏峰值的问题)
    if (ath !== null) {
      t.peakPrice = Math.max(t.peakPrice || 0, ath);
    }

    if (ath === null && high2h === null) continue;
    if (ath === null) ath = high2h;

    if (ath > MAX_HIGH_PRICE) continue;
    if (ageHours <= 1 && ath > MAX_CURRENT_PRICE_YOUNG) continue;

    // 前2h最高价
    const earlyHighLimit = isRelaxed ? MAX_EARLY_HIGH_PRICE_RELAXED : MAX_EARLY_HIGH_PRICE;
    if (ageHours > 1 && high2h !== null && high2h > earlyHighLimit) continue;

    // 现价/最高价比
    if (ath > 0 && currentPrice) {
      const ratio = currentPrice / ath;
      if (ratio < PRICE_RATIO_LOW || ratio > PRICE_RATIO_HIGH) continue;
    }

    // 底价检查
    if (currentPrice && candles && candles.length >= 1) {
      const minPrice = ageHours > 1
        ? calcMinPriceExcludeFirst(candles, createTsSec)
        : calcMinPriceAll(candles);
      if (minPrice && minPrice > 0) {
        const aboveMinRatio = currentPrice / minPrice - 1;
        if (aboveMinRatio < 0.10 || aboveMinRatio > 1.00) continue;
      }
    }

    results.push({ ...t, ath, high2h, walletSignals: wa ? wa.signals : [], walletAnalysis: wa || null });
    const sigStr = wa && wa.signals.length > 0 ? ` 💰 ${wa.signals.join(", ")}` : "";
    console.log(`[精筛] ✓ ${t.name || t.address.slice(0, 16)} — ATH ${ath.toExponential(3)}, 现价 ${currentPrice.toExponential(3)}, 持币 ${t.holders}${sigStr}`);
  }

  return results;
}

// ===================================================================
//  热点匹配 (保留 v1 逻辑)
// ===================================================================
const HOTSPOT_CACHE_TTL = 900_000;
let hotspotCache = { ts: 0, keywords: [] };

function normalize(text) {
  return text.toLowerCase().trim().replace(/[_\-./·・\s]+/g, " ");
}

async function fetchWeiboHot() {
  try {
    const res = await fetchJSON("https://weibo.com/ajax/side/hotSearch", {
      headers: { "User-Agent": "Mozilla/5.0", Referer: "https://weibo.com/", "X-Requested-With": "XMLHttpRequest", Accept: "application/json" },
      timeout: 10000,
    });
    if (!res.data || !res.data.data) return [];
    const items = res.data.data.realtime || [];
    return items.filter(i => (i.word || "").trim().length >= 2).map((i, idx) => ({ word: i.word.trim(), rank: idx, source: "weibo" }));
  } catch (e) { return []; }
}

async function fetchAllHotspots() {
  const now = Date.now();
  if (now - hotspotCache.ts < HOTSPOT_CACHE_TTL && hotspotCache.keywords.length > 0) return hotspotCache.keywords;
  const all = await fetchWeiboHot();
  // 简化: 只用微博热搜, 省掉 Google Trends 和 Twitter (节省时间)
  console.log(`[HOTSPOT] 热点: ${all.length} 个关键词`);
  hotspotCache = { ts: now, keywords: all };
  return all;
}

function hotspotMatch(token, hotspots, descr = "") {
  const name = normalize(token.name || "");
  const short = normalize(token.symbol || "");
  const desc = normalize(descr);
  let score = 0;
  const matched = [];
  const seenWords = new Set();
  for (const h of hotspots) {
    const wordLower = normalize(h.word);
    if (seenWords.has(wordLower)) continue;
    if (wordLower.length <= 3) {
      if (wordLower !== name && wordLower !== short) continue;
    } else {
      let found = name.includes(wordLower) || short.includes(wordLower) || (desc && desc.includes(wordLower));
      if (!found && name.length >= 2) found = wordLower.includes(name) || wordLower.includes(short);
      if (!found) continue;
    }
    seenWords.add(wordLower);
    const rankWeight = Math.max(0.5, 1.0 - h.rank * 0.01);
    score += rankWeight * 1.2;
    matched.push(`${h.word}(${h.source})`);
  }
  return { score, matched, isHot: matched.length > 0 };
}

// ===================================================================
//  Main
// ===================================================================
async function main() {
  const scanStart = Date.now();
  const scanTime = new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }).replace(/\//g, "-");
  console.log(`\n========== SCAN v2 START: ${scanTime} ==========`);

  const nowMs = Date.now();

  // 加载队列
  const queueState = loadQueue();
  const existingAddrs = new Set([
    ...queueState.tokens.map(t => t.address),
    ...queueState.eliminated.map(t => t.address),
  ]);

  // Step 1: 链上发现
  console.log("\n--- Step 1: 链上发现 ---");
  const { tokens: newOnChain, latestBlock } = await discoverOnChain(queueState.lastBlock);

  // Step 2: 入场筛
  console.log("\n--- Step 2: 入场筛 ---");
  const { admitted, rejected: rejectedAtEntry } = await admissionFilter(newOnChain, existingAddrs);

  // 将通过入场筛的代币加入队列 (用 RPC Transfer 事件查持币数)
  if (admitted.length > 0) {
    const newTokenInfos = admitted.map(a => ({ address: a.token.address, block: a.token.block || 0, createdAt: a.token.createdAt || 0 }));
    const newRpcHolders = await rpcHolderCounts(newTokenInfos);
    for (const { token, detail } of admitted) {
      const rpcH = newRpcHolders.get(token.address);
      const initHolders = rpcH != null ? rpcH : detail.holders;
      queueState.tokens.push({
        address: token.address,
        creator: token.creator || "",
        block: token.block || 0,
        name: detail.name || token.name || "",
        symbol: detail.shortName || token.symbol || "",
        createdAt: token.createdAt,
        addedAt: nowMs,
        totalSupply: detail.totalSupply,
        socialCount: detail.socialCount,
        socialLinks: detail.socialLinks,
        descr: detail.descr,
        price: detail.price,
        peakPrice: detail.price,
        holders: initHolders,
        peakHolders: initHolders,
        liquidity: detail.liquidity || 0,
        peakLiquidity: detail.liquidity || 0,
        raisedAmount: detail.raisedAmount || 0,
        marketCap: detail.marketCap || 0,
        progress: detail.progress || 0,
        day1Vol: detail.day1Vol || 0,
        consecDrops: 0,
        lastPrice: detail.price,
      });
    }
  }

  console.log(`[QUEUE] 入队后: ${queueState.tokens.length} 个代币`);

  // Step 3: 淘汰检查
  console.log("\n--- Step 3: 淘汰检查 ---");
  const { survivors, eliminated } = await eliminationCheck(queueState.tokens, nowMs);
  queueState.tokens = survivors;
  queueState.eliminated.push(...eliminated.map(e => ({
    address: e.address, name: e.name, elimReason: e.elimReason,
    eliminatedAt: e.eliminatedAt, createdAt: e.createdAt,
  })));

  console.log(`[QUEUE] 淘汰后: ${survivors.length} 个存活, ${eliminated.length} 个淘汰`);

  // Step 4: 钱包分析 + 精筛 + 热点匹配
  console.log("\n--- Step 4: 钱包分析 + 精筛 ---");

  // 钱包行为分析 (开发者+聪明钱) 与 热点获取 并行
  let walletMap = new Map();
  let hotspots = [];
  const hotspotPromise = fetchAllHotspots(); // 提前启动热点获取

  if (BSCSCAN_API_KEY && survivors.length > 0) {
    // 加载聪明钱地址 (手动配置 + 自动发现)
    const smartAddresses = await loadSmartMoneyAddresses();
    console.log(`[钱包] 分析 ${survivors.length} 个代币的开发者/聪明钱行为...`);
    walletMap = await batchWalletAnalysis(survivors, smartAddresses);
    const excludedCount = [...walletMap.values()].filter(w => w.excluded).length;
    const signalCount = [...walletMap.values()].filter(w => w.signals.length > 0).length;
    console.log(`[钱包] 排除: ${excludedCount}, 有加分信号: ${signalCount}`);
  }

  // 等待热点数据 (大概率已经完成)
  hotspots = await hotspotPromise;

  const qualityResults = await qualityFilter(survivors, nowMs, walletMap);

  // 热点匹配
  for (const t of qualityResults) {
    t.hotNews = hotspotMatch(t, hotspots, t.descr || "");
  }

  // 按持币数排序
  qualityResults.sort((a, b) => (b.holders || 0) - (a.holders || 0));

  console.log(`[精筛] 通过: ${qualityResults.length}/${survivors.length}`);

  // 输出结果 (兼容 build.js 格式)
  const result = {
    scanTime,
    totalTokens: queueState.tokens.length,
    newDiscovered: newOnChain.length,
    newAdmitted: admitted.length,
    eliminatedCount: eliminated.length,
    filteredTokens: qualityResults.length,
    queueSize: survivors.length,
    tokens: qualityResults.map(t => {
      const currentPrice = t.price || 0;
      return {
        address: t.address,
        name: t.name || "",
        symbol: t.symbol || "",
        holders: t.holders || 0,
        created_at: t.createdAt,
        total_supply: t.totalSupply || TOTAL_SUPPLY,
        price: currentPrice,
        max_price: t.ath || t.peakPrice || 0,
        high_2h: t.high2h || 0,
        price_ratio: t.ath > 0 ? +(currentPrice / t.ath).toFixed(4) : 0,
        age_hours: +(Math.max(0, (nowMs - t.createdAt) / 3600000)).toFixed(2),
        social_count: t.socialCount || 0,
        social_links: t.socialLinks || {},
        hot_news: t.hotNews?.isHot || false,
        hot_score: t.hotNews?.score || 0,
        hot_keywords: t.hotNews?.matched || [],
        day1_vol: t.day1Vol || 0,
        progress: t.progress || 0,
        liquidity: t.liquidity || 0,
        raised_amount: t.raisedAmount || 0,
        market_cap: t.marketCap || 0,
        wallet_signals: t.walletSignals || [],
      };
    }),
    // 队列快照: 存活代币 + 本轮淘汰代币
    queue: survivors.map(t => ({
      address: t.address,
      name: t.name || "",
      symbol: t.symbol || "",
      holders: t.holders || 0,
      created_at: t.createdAt,
      price: t.price || 0,
      peak_price: t.peakPrice || 0,
      peak_holders: t.peakHolders || 0,
      liquidity: t.liquidity || 0,
      peak_liquidity: t.peakLiquidity || 0,
      raised_amount: t.raisedAmount || 0,
      market_cap: t.marketCap || 0,
      progress: t.progress || 0,
      social_count: t.socialCount || 0,
      social_links: t.socialLinks || {},
      age_hours: +(Math.max(0, (nowMs - t.createdAt) / 3600000)).toFixed(2),
      consec_drops: t.consecDrops || 0,
    })),
    eliminatedThisRound: eliminated.map(t => ({
      address: t.address,
      name: t.name || "",
      symbol: t.symbol || "",
      holders: t.holders || 0,
      created_at: t.createdAt,
      price: t.price || 0,
      peak_price: t.peakPrice || 0,
      reason: t.elimReason || "",
      social_count: t.socialCount || 0,
      age_hours: +(Math.max(0, (nowMs - (t.createdAt || 0)) / 3600000)).toFixed(2),
    })),
    rejectedAtEntry: rejectedAtEntry.map(({ token: t, detail, reason }) => ({
      address: t.address,
      name: (detail && detail.name) || t.name || "",
      symbol: (detail && detail.shortName) || t.symbol || "",
      created_at: t.createdAt,
      reason: reason || "",
      holders: (detail && detail.holders) || 0,
      price: (detail && detail.price) || 0,
      social_count: (detail && detail.socialCount) || 0,
      social_links: (detail && detail.socialLinks) || {},
      progress: (detail && detail.progress) || 0,
      age_hours: +(Math.max(0, (nowMs - (t.createdAt || 0)) / 3600000)).toFixed(2),
    })),
  };

  // 写入 data/
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  const bjNow = new Date(Date.now() + 8 * 3600 * 1000);
  const scanId = bjNow.toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const scanFile = path.join(DATA_DIR, `${scanId}.json`);
  fs.writeFileSync(scanFile, JSON.stringify(result, null, 2));

  // 更新队列状态
  queueState.lastBlock = latestBlock;
  queueState.lastScanTime = nowMs;
  saveQueue(queueState);

  // 清理 7 天前的数据文件
  const cutoffMs = Date.now() - 7 * 24 * 3600 * 1000;
  const dataFiles = fs.readdirSync(DATA_DIR).filter(f => f.endsWith(".json") && f !== "queue.json");
  let cleaned = 0;
  for (const f of dataFiles) {
    const match = f.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})\.json$/);
    if (!match) continue;
    const [, y, mo, d, h, mi, s] = match;
    const fileDate = new Date(`${y}-${mo}-${d}T${h}:${mi}:${s}Z`);
    if (fileDate.getTime() < cutoffMs) { fs.unlinkSync(path.join(DATA_DIR, f)); cleaned++; }
  }
  if (cleaned > 0) console.log(`[SCAN] 清理 ${cleaned} 个旧文件`);

  const elapsed = ((Date.now() - scanStart) / 1000).toFixed(1);
  console.log(`\n========== SCAN v2 DONE: ${elapsed}s ==========`);
  console.log(`链上发现: ${newOnChain.length} | 入场: ${admitted.length} | 入场淘汰: ${rejectedAtEntry.length} | 队列: ${survivors.length} | 淘汰: ${eliminated.length} | 精筛通过: ${qualityResults.length}`);
}

main().catch(e => {
  console.error("[SCAN] Fatal error:", e);
  process.exit(1);
});
