/**
 * 比对脚本: four.meme API 返回的代币 vs 链上实际创建的代币
 *
 * 思路:
 *   1. 通过 BSC RPC eth_getLogs 查询 four.meme 合约的代币创建事件
 *   2. 通过 four.meme search API 拉取所有代币
 *   3. 按时间过滤 (近3天), 比对两者差异
 *
 * 用法: node scripts/compare.js
 */

const https = require("https");
const http = require("http");
const fs = require("fs");
const path = require("path");

// === Config ===
const LOCAL_CONFIG_PATH = path.join(__dirname, "..", "config.local.json");
let localConfig = {};
try {
  if (fs.existsSync(LOCAL_CONFIG_PATH)) {
    localConfig = JSON.parse(fs.readFileSync(LOCAL_CONFIG_PATH, "utf-8"));
  }
} catch (e) {}

let proxyConfig = null;
if (localConfig.proxy && localConfig.proxy.enabled) {
  proxyConfig = localConfig.proxy;
  console.log(`[PROXY] 代理: ${proxyConfig.host}:${proxyConfig.port}`);
}

// four.meme 合约地址 (exchange proxy)
const FOUR_MEME_CONTRACT = "0x5c952063c7fc8610ffdb798152d69f0b9550762b";

// 代币创建事件的 topic0 (从链上实际观察到的)
// 0x396d5e90... 是真正的 TokenCreated 事件, data[1] 包含代币地址
const TOKEN_CREATE_TOPIC = "0x396d5e902b675b032348d3d2e9517ee8f0c4a926603fbc075d3d282ff00cad20";

const BSC_RPC = "https://bsc-rpc.publicnode.com/";

// === HTTP Helpers ===
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
      const req = mod.request(reqOpts, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try { resolve({ status: res.statusCode, data: JSON.parse(data) }); }
          catch { resolve({ status: res.statusCode, data: null, raw: data }); }
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
        method: "CONNECT",
        path: `${urlObj.hostname}:${urlObj.port || 443}`,
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

// ===================================================================
//  链上数据: 通过 BSC RPC 查询 four.meme 合约的代币创建事件
// ===================================================================
async function getOnChainTokens(fromBlock, toBlock) {
  const tokens = new Map(); // address -> { block, txHash }
  let skippedNonFm = 0; // 不符合 four.meme 后缀的计数
  const CHUNK_SIZE = 10000; // 每次查询的区块范围

  /** 判断地址是否为 four.meme 平台代币 (后缀 4444 或 ffff) */
  function isFourMemeToken(addr) {
    return addr.endsWith("4444") || addr.endsWith("ffff");
  }

  /** 从单批日志中提取 four.meme 代币 */
  function extractTokens(logs) {
    for (const log of logs) {
      const data = log.data.slice(2);
      // data[1] = token address (word index 1, hex offset 88-128)
      const tokenAddr = ("0x" + data.slice(88, 128)).toLowerCase();
      if (!isFourMemeToken(tokenAddr)) {
        skippedNonFm++;
        continue;
      }
      if (!tokens.has(tokenAddr)) {
        tokens.set(tokenAddr, {
          block: parseInt(log.blockNumber, 16),
          txHash: log.transactionHash,
        });
      }
    }
  }

  let current = fromBlock;
  while (current <= toBlock) {
    const end = Math.min(current + CHUNK_SIZE - 1, toBlock);
    const fromHex = "0x" + current.toString(16);
    const toHex = "0x" + end.toString(16);

    try {
      const res = await rpcCall("eth_getLogs", [{
        address: FOUR_MEME_CONTRACT,
        fromBlock: fromHex,
        toBlock: toHex,
        topics: [TOKEN_CREATE_TOPIC],
      }]);

      if (res.error) {
        if (res.error.code === -32005 || (res.error.message && res.error.message.includes("limit"))) {
          // 范围太大, 缩小一半
          const mid = current + Math.floor((end - current) / 2);
          if (mid === current) {
            console.warn(`[CHAIN] 单个区块 ${current} 也超限, 跳过`);
            current = end + 1;
            continue;
          }
          const res2 = await rpcCall("eth_getLogs", [{
            address: FOUR_MEME_CONTRACT,
            fromBlock: fromHex,
            toBlock: "0x" + mid.toString(16),
            topics: [TOKEN_CREATE_TOPIC],
          }]);
          if (!res2.error && res2.result) {
            extractTokens(res2.result);
          }
          current = mid + 1;
          await sleep(200);
          continue;
        }
        console.warn(`[CHAIN] RPC error at block ${current}-${end}:`, res.error.message || res.error);
        // 如果是历史裁剪错误, 跳过更大范围 (50000 blocks ≈ 6h)
        if (res.error.message && res.error.message.includes("pruned")) {
          current = end + 50000;
        } else {
          current = end + 1;
        }
        continue;
      }

      const logs = res.result || [];
      extractTokens(logs);

      process.stdout.write(`\r[CHAIN] 扫描中... block ${current}-${end}, 已发现 ${tokens.size} 个代币    `);
    } catch (e) {
      console.warn(`\n[CHAIN] 请求失败 block ${current}-${end}: ${e.message}`);
      await sleep(1000);
    }

    current = end + 1;
    await sleep(100);
  }

  console.log(`\n[CHAIN] 链上扫描完成: ${tokens.size} 个 four.meme 代币 (跳过 ${skippedNonFm} 个非 four.meme 地址)`);
  return tokens;
}

// ===================================================================
//  four.meme API: 拉取所有代币
// ===================================================================
async function fetchAllFmTokens() {
  const FM_SEARCH = "https://four.meme/meme-api/v1/public/token/search";
  const FM_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    Accept: "application/json",
    Origin: "https://four.meme",
    Referer: "https://four.meme/",
  };

  const seen = new Map();
  const pageSize = 100;
  const maxPages = 50;

  async function fetchPages(query, label) {
    for (let page = 1; page <= maxPages; page++) {
      const payload = { pageIndex: page, pageSize, ...query };
      try {
        const res = await fetchJSON(FM_SEARCH, {
          method: "POST",
          headers: FM_HEADERS,
          body: JSON.stringify(payload),
        });
        if (!res.data || res.data.code !== 0) break;
        const items = res.data.data || [];
        if (items.length === 0) break;

        for (const t of items) {
          const addr = (t.tokenAddress || "").toLowerCase();
          if (addr && !seen.has(addr)) {
            seen.set(addr, {
              name: t.name || "",
              shortName: t.shortName || "",
              createDate: parseInt(t.createDate || 0),
              price: parseFloat(t.price || 0),
              hold: parseInt(t.hold || 0),
              status: t.status || "",
            });
          }
        }

        if (items.length < pageSize) break;
      } catch (e) {
        console.error(`[FM] ${label} p${page}: ${e.message}`);
        break;
      }
      await sleep(250);
    }
  }

  console.log("[FM] 拉取 four.meme 所有代币...");
  const symbols = ["BNB", "USD1", "USDT", "CAKE"];

  for (const sym of symbols) {
    process.stdout.write(`\r[FM] NEW/DESC/PUBLISH/${sym}... (${seen.size})`);
    await fetchPages({ type: "NEW", listType: "NOR", sort: "DESC", status: "PUBLISH", symbol: sym }, `NEW/DESC/PUB/${sym}`);
  }
  for (const sym of symbols) {
    process.stdout.write(`\r[FM] NEW/ASC/PUBLISH/${sym}... (${seen.size})`);
    await fetchPages({ type: "NEW", listType: "NOR", sort: "ASC", status: "PUBLISH", symbol: sym }, `NEW/ASC/PUB/${sym}`);
  }
  for (const sym of symbols) {
    process.stdout.write(`\r[FM] NEW/DESC/TRADE/${sym}... (${seen.size})`);
    await fetchPages({ type: "NEW", listType: "NOR_DEX", sort: "DESC", status: "TRADE", symbol: sym }, `NEW/DESC/TRADE/${sym}`);
  }
  for (const sym of symbols) {
    process.stdout.write(`\r[FM] NEW/ASC/TRADE/${sym}... (${seen.size})`);
    await fetchPages({ type: "NEW", listType: "NOR_DEX", sort: "ASC", status: "TRADE", symbol: sym }, `NEW/ASC/TRADE/${sym}`);
  }
  for (const [sortType, listType] of [["HOT", "ADV"], ["VOL", "NOR"], ["PROGRESS", "NOR"]]) {
    for (const [status, lt] of [["PUBLISH", listType], ["TRADE", "NOR_DEX"]]) {
      process.stdout.write(`\r[FM] ${sortType}/${status}... (${seen.size})          `);
      await fetchPages({ type: sortType, listType: lt, status }, `${sortType}/${status}`);
    }
  }

  console.log(`\n[FM] four.meme API 总计: ${seen.size} 个不重复代币`);
  return seen;
}

// ===================================================================
//  获取区块时间戳
// ===================================================================
async function getBlockTimestamp(blockNumber) {
  const res = await rpcCall("eth_getBlockByNumber", ["0x" + blockNumber.toString(16), false]);
  if (res.result && res.result.timestamp) {
    return parseInt(res.result.timestamp, 16);
  }
  return null;
}

// ===================================================================
//  Main
// ===================================================================
async function main() {
  const COMPARE_HOURS = 72;
  const nowMs = Date.now();
  const startMs = nowMs - COMPARE_HOURS * 3600 * 1000;

  console.log("========== 比对: four.meme API vs 链上数据 ==========");
  console.log(`时间范围: 近 ${COMPARE_HOURS} 小时`);
  console.log(`起始: ${new Date(startMs).toISOString()}`);
  console.log(`结束: ${new Date(nowMs).toISOString()}`);
  console.log("");

  // 获取最新区块号和实际区块时间
  const blockRes = await rpcCall("eth_blockNumber", []);
  const latestBlock = parseInt(blockRes.result, 16);

  // 动态计算区块时间 (BSC 已从3s降至~0.45s)
  const refBlock = latestBlock - 50000;
  const [latestInfo, refInfo] = await Promise.all([
    rpcCall("eth_getBlockByNumber", ["0x" + latestBlock.toString(16), false]),
    rpcCall("eth_getBlockByNumber", ["0x" + refBlock.toString(16), false]),
  ]);
  const latestTs = parseInt(latestInfo.result.timestamp, 16);
  const refTs = parseInt(refInfo.result.timestamp, 16);
  const blockTime = (latestTs - refTs) / 50000;
  console.log(`[CHAIN] 实际区块时间: ${blockTime.toFixed(3)}s`);

  const blocksNeeded = Math.ceil(COMPARE_HOURS * 3600 / blockTime) + 2000; // 余量
  const estimatedStartBlock = latestBlock - blocksNeeded;
  console.log(`[CHAIN] 区块范围: ${estimatedStartBlock} ~ ${latestBlock} (约 ${latestBlock - estimatedStartBlock} 个区块)`);
  console.log("");

  // 并行: 链上扫描 + four.meme API
  const [onChainTokens, fmTokens] = await Promise.all([
    getOnChainTokens(estimatedStartBlock, latestBlock),
    fetchAllFmTokens(),
  ]);

  // 确定链上实际覆盖的时间范围 (公共 RPC 可能裁剪了旧区块)
  let chainEarliestBlock = Infinity;
  for (const [, info] of onChainTokens) {
    if (info.block < chainEarliestBlock) chainEarliestBlock = info.block;
  }
  let chainStartMs = startMs;
  if (chainEarliestBlock < Infinity) {
    const ts = await getBlockTimestamp(chainEarliestBlock);
    if (ts) {
      chainStartMs = ts * 1000;
      const actualHours = ((nowMs - chainStartMs) / 3600000).toFixed(1);
      console.log(`[CHAIN] 实际覆盖时间: ${new Date(chainStartMs).toISOString()} ~ now (${actualHours}h)`);
      if (chainStartMs > startMs) {
        console.log(`[CHAIN] ⚠️  公共 RPC 历史被裁剪, 实际只覆盖了 ${actualHours}h (目标 ${COMPARE_HOURS}h)`);
      }
    }
  }

  // 按链上实际覆盖的时间范围过滤 four.meme 数据, 确保时间窗口一致
  const fmRecent = new Map();
  for (const [addr, info] of fmTokens) {
    if (info.createDate >= chainStartMs && (addr.endsWith("4444") || addr.endsWith("ffff"))) {
      fmRecent.set(addr, info);
    }
  }
  const actualHours = ((nowMs - chainStartMs) / 3600000).toFixed(1);

  console.log("");
  console.log("========== 比对结果 ==========");
  console.log(`链上 four.meme 代币数 (RPC事件, 后缀4444/ffff): ${onChainTokens.size}`);
  console.log(`four.meme API 代币数 (同时间窗口 ${actualHours}h, 后缀4444/ffff): ${fmRecent.size}`);
  console.log(`four.meme API 代币总数 (所有时间): ${fmTokens.size}`);

  // 比对
  const both = [];
  const onlyOnChain = [];
  const onlyFm = [];

  for (const [addr, info] of onChainTokens) {
    if (fmRecent.has(addr)) {
      both.push(addr);
    } else {
      onlyOnChain.push({ address: addr, ...info });
    }
  }

  for (const [addr, info] of fmRecent) {
    if (!onChainTokens.has(addr)) {
      onlyFm.push({ address: addr, ...info });
    }
  }

  console.log("");
  console.log(`两者都有: ${both.length}`);
  console.log(`仅链上有 (four.meme API 遗漏): ${onlyOnChain.length}`);
  console.log(`仅 four.meme 有 (链上未找到创建事件): ${onlyFm.length}`);

  if (onlyOnChain.length > 0) {
    console.log("");
    console.log("--- 链上有但 four.meme API 遗漏的代币 (前30个) ---");
    // 获取部分代币的区块时间戳
    const show = onlyOnChain.slice(0, 30);
    for (let i = 0; i < show.length; i++) {
      const t = show[i];
      let timeStr = "?";
      try {
        const ts = await getBlockTimestamp(t.block);
        if (ts) timeStr = new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 19);
      } catch (e) {}
      console.log(`  ${i + 1}. ${t.address} | block: ${t.block} | 时间: ${timeStr}`);
      if (i < show.length - 1) await sleep(100);
    }
    if (onlyOnChain.length > 30) {
      console.log(`  ... 还有 ${onlyOnChain.length - 30} 个`);
    }
  }

  if (onlyFm.length > 0 && onlyFm.length <= 20) {
    console.log("");
    console.log("--- 仅 four.meme API 有但链上未找到创建事件的代币 ---");
    for (const t of onlyFm) {
      const time = t.createDate ? new Date(t.createDate).toISOString().replace("T", " ").slice(0, 19) : "?";
      console.log(`  ${t.address} | ${t.name} (${t.shortName}) | 创建: ${time}`);
    }
  }

  // 覆盖率
  const coverage = onChainTokens.size > 0
    ? ((both.length / onChainTokens.size) * 100).toFixed(2)
    : "N/A";

  console.log("");
  console.log("========== 总结 ==========");
  console.log(`four.meme API 对链上代币的覆盖率: ${coverage}%`);
  console.log(`链上总数: ${onChainTokens.size}, API覆盖: ${both.length}, 遗漏: ${onlyOnChain.length}`);

  if (onlyOnChain.length > 0) {
    const pct = ((onlyOnChain.length / onChainTokens.size) * 100).toFixed(1);
    console.log(`\n⚠️  four.meme API 遗漏了 ${onlyOnChain.length} 个链上代币 (${pct}%)`);
  } else {
    console.log("\n✅ four.meme API 完整覆盖了链上所有代币");
  }

  // 保存结果
  const resultFile = path.join(__dirname, "..", "compare-result.json");
  const result = {
    compareTime: new Date().toISOString(),
    timeRangeHours: COMPARE_HOURS,
    blockRange: { start: estimatedStartBlock, end: latestBlock },
    onChainCount: onChainTokens.size,
    fmApiCount: fmRecent.size,
    fmApiTotalCount: fmTokens.size,
    bothCount: both.length,
    onlyOnChainCount: onlyOnChain.length,
    onlyFmCount: onlyFm.length,
    coveragePercent: parseFloat(coverage) || 0,
    onlyOnChain: onlyOnChain.map(t => ({
      address: t.address,
      block: t.block,
      txHash: t.txHash,
    })),
    onlyFm: onlyFm.slice(0, 50).map(t => ({
      address: t.address,
      name: t.name,
      shortName: t.shortName,
      createdAt: t.createDate ? new Date(t.createDate).toISOString() : null,
    })),
  };
  fs.writeFileSync(resultFile, JSON.stringify(result, null, 2));
  console.log(`\n详细结果已保存到: ${resultFile}`);
}

main().catch(e => {
  console.error("[COMPARE] Fatal:", e);
  process.exit(1);
});
