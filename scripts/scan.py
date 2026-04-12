"""
BSC Token Scanner v6 — 极速扫描, 以快致胜 (Python 版, GitHub Actions 专用)

数据源: BSC RPC (链上事件) + four.meme API (详情/持币数) + DexScreener (价格)

与 token_trading/scanner.py 的区别:
  - 配置从 config.local.json 加载 (proxy 格式: {host, port})
  - 无 Telegram 推送, 无交易模块
  - 输出 JSON 文件到 data/ 目录 (带时间戳)
  - 队列文件: data/queue.json
  - 入场筛同时记录被拒代币 (前端 "入场淘汰" tab)
  - 日志仅输出到 stdout

v6 架构: 极速扫描 (1 分钟一轮)
  1. 链上发现 (~1s): BSC RPC eth_getLogs → four.meme V1 合约 TokenCreated 事件 → 新代币地址
  2. 入场筛 (~数秒): four.meme Detail API → 淘汰无社交 / 总量≠10亿 / 币龄>5min
  3. 淘汰检查 (~数秒): DexScreener 批量查价 + Detail API 查持币数 → 永久淘汰弃盘币
  4. 精筛 (瞬时): 极简三条件直接过滤
  5. 仿盘检测: 本地统计同名代币数量 (零 API 调用), 有大量仿盘(≥3)则标记

砍掉的慢环节 (v5 → v6):
  - GeckoTerminal K线 (每个代币 2s+)
  - BSCScan 钱包行为分析 (开发者/聪明钱)
  - 币安 Web3 聪明钱信号 + Token Dynamic
  - GMGN 聪明钱地址
  - RPC 持币数统计 (改用 four.meme detail 的 holderCount)

淘汰条件 (永久剔除):
  - 价格从峰值跌 90%+
  - 持币地址从 ≥30 跌破 10
  - 无社交媒体
  - 流动性从 >$1k 跌破 $100
  - 进度 < 1% 且币龄 > 2h
  - 进度 < 5% 且币龄 > 4h
  - 币龄 > 15min 且最高持币数 < 3
  - 币龄 > 1h 且最高持币数 < 5
  - 币龄 > 48h

精筛条件 (两档):
  快档 (币龄 ≤ 5min):
  - 持币地址数 ≥ 30
  - 当前价 < $0.000006
  慢档 (币龄 > 5min 且 ≤ 15min):
  - 持币地址数 ≥ 20
  - 当前价 < $0.000008
  通用趋势条件 (两档共用):
  - 持币地址数近 2 轮扫描递增 (不足 2 轮不通过)
  - 价格近 2 轮扫描递增 (不足 2 轮不通过)
"""

from __future__ import annotations

import json
import time
import logging
import re
import sys
import os
import uuid
import glob
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timezone
from pathlib import Path


# ===================================================================
#  日志 (仅 stdout)
# ===================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ===================================================================
#  配置
# ===================================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.local.json"
DATA_DIR = PROJECT_ROOT / "data"
QUEUE_FILE = DATA_DIR / "queue.json"


def load_config() -> dict:
    """加载 config.local.json 并转换 proxy 格式, 支持环境变量回退"""
    cfg = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    # 环境变量覆盖
    if os.environ.get("BSCSCAN_API_KEY"):
        cfg["bscscanApiKey"] = os.environ["BSCSCAN_API_KEY"]
    # 转换 proxy 格式: {host, port} → {enabled, http, https}
    raw_proxy = cfg.get("proxy")
    if raw_proxy and raw_proxy.get("enabled"):
        host = raw_proxy.get("host", "127.0.0.1")
        port = raw_proxy.get("port", 7890)
        proxy_url = f"http://{host}:{port}"
        cfg["proxy"] = {"enabled": True, "http": proxy_url, "https": proxy_url}
    elif raw_proxy:
        cfg["proxy"] = {"enabled": False, "http": "", "https": ""}
    return cfg


# ===================================================================
#  常量 & API
# ===================================================================
FM_SEARCH = "https://four.meme/meme-api/v1/public/token/search"
FM_DETAIL = "https://four.meme/meme-api/v1/private/token/get/v2"
FM_TICKER = "https://four.meme/meme-api/v1/public/ticker"
GT_BASE = "https://api.geckoterminal.com/api/v2"
DS_BASE = "https://api.dexscreener.com"
BSCSCAN_API = "https://api.etherscan.io/v2/api"  # Etherscan V2 API (支持 chainid 参数)
BSC_RPC = "https://bsc-rpc.publicnode.com/"
BINANCE_SMART_SIGNAL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai"
BINANCE_TOKEN_DYNAMIC = "https://web3.binance.com/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai"
BINANCE_TOKEN_META = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai"

# four.meme 合约地址 (来源: four-flap-meme-sdk v1.9.48)
# TokenManagerOriginal: 0x5c952063c7fc8610FFDB798152D69F0B9550762b (当前唯一活跃合约)
# TokenManagerV1:       0xf7F823d0E790219dBf727bDb971837574655fCB0 (已废弃, 无事件)
# TokenManagerV2:       0x342399a59943B5815849657Aa0e06D7058D9d5C6 (已废弃, 无事件)
# TokenManagerHelper3:  0xF251F83e40a78868FcfA3FA4599Dad6494E46034 (查询辅助合约)
FOUR_MEME_CONTRACT = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"       # TokenManagerOriginal (所有 TokenCreate 事件都在此合约上)
TOKEN_CREATE_TOPIC = "0x396d5e902b675b032348d3d2e9517ee8f0c4a926603fbc075d3d282ff00cad20"      # 旧版 TokenCreate (12 words, 含名称/符号/时间戳, 少量使用)
TOKEN_CREATE_TOPIC_V3 = "0x0a5575b3648bae2210cee56bf33254cc1ddfbc7bf637c0af2ac18b14fb1bae19"  # 新版 TokenCreate (8 words, 无名称/时间戳, 当前主力)
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# 所有 TokenCreate 事件监听配置 (新增事件只需在此追加)
# 注意: 0x7db52723... 是买卖事件 (同一代币多次出现), 不要监听
FOUR_MEME_FACTORIES = [
    (FOUR_MEME_CONTRACT, TOKEN_CREATE_TOPIC),       # 旧版 TokenCreate
    (FOUR_MEME_CONTRACT, TOKEN_CREATE_TOPIC_V3),    # 新版 TokenCreate (同一合约, 不同 topic)
]

FM_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://four.meme",
    "Referer": "https://four.meme/",
}
GT_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
DS_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
BINANCE_HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/1.1 (Skill)",
}

# 精筛阈值
MAX_AGE_HOURS = 48
SCAN_INTERVAL_MIN = 15                 # GitHub Actions cron 间隔 (分钟)
TOTAL_SUPPLY = 1_000_000_000           # 10亿
QUALITY_MAX_AGE_MIN = 5                # 精筛(快档): 币龄 ≤ 5 分钟
QUALITY_MIN_HOLDERS = 30               # 精筛(快档): 持币地址数 ≥ 30
QUALITY_MAX_PRICE = 0.000006           # 精筛(快档): 当前价 < $0.000006
QUALITY_SLOW_MAX_AGE_MIN = 15          # 精筛(慢档): 币龄 ≤ 15 分钟
QUALITY_SLOW_MIN_HOLDERS = 20          # 精筛(慢档): 持币地址数 ≥ 20
QUALITY_SLOW_MAX_PRICE = 0.000008      # 精筛(慢档): 当前价 < $0.000008
COPYCAT_MARK_MIN = 3                   # 仿盘数 ≥3 标记
MIN_SOCIAL_COUNT = 1                   # 最少关联社交媒体数

# 淘汰阈值
ELIM_PRICE_DROP_PCT = 0.90             # 价格从峰值跌 90%
ELIM_HOLDERS_FLOOR = 10               # 持币数跌破 10
ELIM_HOLDERS_PEAK_MIN = 30            # 持币数曾达到 30 才触发跌破淘汰
ELIM_LIQ_FLOOR = 100                  # 流动性跌破 $100
ELIM_LIQ_PEAK_MIN = 1000              # 流动性曾达到 $1000 才触发跌破淘汰
ELIM_PROGRESS_MIN = 0.01              # 进度 < 1%
ELIM_PROGRESS_AGE_HOURS = 2           # 进度<1%淘汰的币龄门槛
ELIM_PROGRESS_MIN_MID = 0.05          # 进度 < 5%
ELIM_PROGRESS_AGE_HOURS_MID = 4       # 进度<5%淘汰的币龄门槛
ELIM_EARLY_PEAK_HOLDERS = 3           # 币龄>15min 最高持币数 < 3 淘汰
ELIM_EARLY_AGE_MIN = 0.25             # 15 分钟 = 0.25h
ELIM_MID_PEAK_HOLDERS = 5             # 币龄>1h 最高持币数 < 5 淘汰
ELIM_MID_AGE_HOURS = 1                # 1 小时


# 已知的 DEX Router 地址 (用于识别买入/卖出行为)
KNOWN_DEX_ROUTERS = {
    "0x10ed43c718714eb63d5aa57b78b54704e256024e",  # PancakeSwap V2 Router
    "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",  # PancakeSwap V3 Router
    "0x1b81d678ffb9c0263b24a97847620c99d213eb14",  # PancakeSwap Universal Router
}

# 零地址/死地址 (转到这些地址不算卖出, 视为销毁)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"
BURN_ADDRESSES = {ZERO_ADDRESS, DEAD_ADDRESS}

# 排除的已知非聪明钱地址 (交易所/合约/稳定币等)
KNOWN_EXCLUDE_ADDRESSES = {
    ZERO_ADDRESS, DEAD_ADDRESS,
    "0x10ed43c718714eb63d5aa57b78b54704e256024e",  # PancakeSwap V2 Router
    "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",  # PancakeSwap V3 Router
    "0x1b81d678ffb9c0263b24a97847620c99d213eb14",  # PancakeSwap Universal Router
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    "0x55d398326f99059ff775485246999027b3197955",  # USDT
    "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
    FOUR_MEME_CONTRACT.lower(),
    "0xd36b6d646ac6e23672e9eedec558164c7f2d6deb",  # four.meme 交易代理合约 (非工厂)
}


# ===================================================================
#  HTTP Session
# ===================================================================
def _build_session(proxy_cfg: dict | None = None,
                   extra_headers: dict | None = None) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if extra_headers:
        session.headers.update(extra_headers)
    if proxy_cfg and proxy_cfg.get("enabled"):
        session.proxies = {
            "http": proxy_cfg.get("http", ""),
            "https": proxy_cfg.get("https", ""),
        }
        log.info("代理已启用: %s", proxy_cfg.get("https", ""))
    return session


_fm_session: requests.Session = None  # type: ignore
_gt_session: requests.Session = None  # type: ignore
_bsc_session: requests.Session = None  # type: ignore
_bn_session: requests.Session = None  # type: ignore


def _ensure_sessions():
    global _fm_session, _gt_session, _bsc_session, _bn_session
    if _fm_session is None:
        try:
            cfg = load_config()
            proxy = cfg.get("proxy")
        except Exception:
            proxy = None
        _fm_session = _build_session(proxy, FM_HEADERS)
        _gt_session = _build_session(proxy, GT_HEADERS)
        _bsc_session = _build_session(proxy, DS_HEADERS)
        _bn_session = _build_session(proxy, BINANCE_HEADERS)


# ===================================================================
#  队列状态管理
# ===================================================================
def load_queue() -> dict:
    """加载队列状态 (从 data/queue.json)"""
    try:
        if QUEUE_FILE.exists():
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            log.info("加载队列: %d 个代币, lastBlock: %s",
                     len(data.get("tokens", [])), data.get("lastBlock", 0))
            return data
    except Exception as e:
        log.warning("队列加载失败: %s", e)
    return {"tokens": [], "eliminated": [], "lastBlock": 0, "lastScanTime": 0,
            "nameIndex": {}}


def save_queue(queue: dict):
    """保存队列状态"""
    # 只保留最近 1000 条淘汰记录
    if len(queue.get("eliminated", [])) > 1000:
        queue["eliminated"] = queue["eliminated"][-1000:]
    # nameIndex 地址列表上限: 每个 key 最多保留 200 个地址 (仿盘检测只需知道数量级)
    name_idx = queue.get("nameIndex", {})
    for key in name_idx:
        addrs = name_idx[key].get("addrs", [])
        if len(addrs) > 200:
            name_idx[key]["addrs"] = addrs[-200:]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


def update_name_index(queue: dict, tokens: list[dict]):
    """
    更新名称索引: 记录所有见过的 name/symbol 及其不同地址数量
    用于仿盘检测, 不受 eliminated 1000 条上限影响
    """
    idx = queue.setdefault("nameIndex", {})
    for t in tokens:
        addr = (t.get("address") or "").lower()
        if not addr:
            continue
        for field in ("symbol", "name"):
            key = _normalize(t.get(field) or "")
            if key and len(key) >= 2:
                entry = idx.setdefault(key, {"addrs": []})
                if addr not in entry["addrs"]:
                    entry["addrs"].append(addr)


# ===================================================================
#  仿盘检测 — 本地队列统计同名/近似名代币
# ===================================================================


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[_\-./·・\s]+", " ", text)
    return text


def detect_copycats(tokens: list[dict],
                    all_known: list[dict],
                    name_index: dict = None) -> dict[str, dict]:
    """
    本地仿盘检测: 在 all_known (队列存活+已淘汰+入场被拒) 中统计同名/近似名代币
    同时参考 name_index (持久化的名称索引, 不受 eliminated 1000 条上限影响)
    返回 {address_lower: {count, isCopycat}}
    count: 同名代币数量 (不含自身)
    isCopycat: count >= 3 时标记为有大量仿盘

    匹配逻辑:
    1. 精确匹配: name/symbol 完全相同
    2. 包含匹配: A 的关键词包含在 B 的关键词中, 或反过来
       (被包含的词长度 ≥ 4 才触发, 避免 "AI" 等短词误匹配)
    name 和 symbol 统一建索引, 交叉匹配
    """
    result: dict[str, dict] = {}
    if not tokens:
        return result

    # 包含匹配最小长度: 中文2字符已有辨识度, 英文需要4字符避免 "AI"/"CZ" 误匹配
    def _min_len(s: str) -> int:
        return 2 if any(ord(c) > 127 for c in s) else 4

    # 统一索引: keyword_lower -> set(addresses)
    keyword_index: dict[str, set[str]] = {}

    # 从 all_known 列表建索引
    for t in (all_known or []):
        addr = (t.get("address") or "").lower()
        if not addr:
            continue
        for field in ("symbol", "name"):
            key = _normalize(t.get(field) or "")
            if key and len(key) >= 2:
                keyword_index.setdefault(key, set()).add(addr)

    # 合并持久化的 nameIndex (补充被 eliminated 1000 条上限截断的历史数据)
    if name_index:
        for key, entry in name_index.items():
            addrs = entry.get("addrs", [])
            if key and len(key) >= 2 and addrs:
                existing = keyword_index.setdefault(key, set())
                for a in addrs:
                    existing.add(a.lower())

    if not keyword_index:
        return result

    # 预提取所有索引 key 列表 (用于包含匹配遍历)
    all_keys = list(keyword_index.keys())

    # 对目标代币查仿盘数
    for t in tokens:
        addr = (t.get("address") or "").lower()
        related = set()

        # 收集该代币的所有关键词
        my_keys = set()
        for field in ("symbol", "name"):
            key = _normalize(t.get(field) or "")
            if key and len(key) >= 2:
                my_keys.add(key)

        for my_key in my_keys:
            # 精确匹配
            related.update(keyword_index.get(my_key, set()))

            # 包含匹配: 遍历索引中的所有 key
            for idx_key in all_keys:
                if idx_key == my_key:
                    continue  # 精确匹配已处理
                # my_key 包含 idx_key (idx_key 是短词, 如 "悟道" 在 "亏完才能悟道" 中)
                if len(idx_key) >= _min_len(idx_key) and idx_key in my_key:
                    related.update(keyword_index[idx_key])
                # idx_key 包含 my_key (my_key 是短词, 如 "悟道" 被 "亏完才能悟道" 包含)
                if len(my_key) >= _min_len(my_key) and my_key in idx_key:
                    related.update(keyword_index[idx_key])

        related.discard(addr)

        count = len(related)
        result[addr] = {
            "count": count,
            "isCopycat": count >= 3,
        }

    copycat_count = sum(1 for v in result.values() if v["isCopycat"])
    if copycat_count > 0:
        log.info("仿盘检测: %d 个有大量仿盘 (≥3)", copycat_count)

    return result


# ===================================================================
#  four.meme API 层
# ===================================================================
def fm_detail(address: str) -> dict | None:
    """获取代币详情 (four.meme Detail API)"""
    _ensure_sessions()
    try:
        r = _fm_session.get(FM_DETAIL, params={"address": address}, timeout=20)
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0 or not d.get("data"):
            return None
        raw = d["data"]
        tp = raw.get("tokenPrice", {})
        social_links = {}
        if raw.get("twitterUrl"):
            social_links["twitter"] = raw["twitterUrl"]
        if raw.get("telegramUrl"):
            social_links["telegram"] = raw["telegramUrl"]
        if raw.get("webUrl"):
            social_links["website"] = raw["webUrl"]
        # 提取创建时间 (毫秒): 优先 launchTime, 回退 createTime / createdAt
        launch_ts = int(raw.get("launchTime", 0) or 0)
        if launch_ts <= 0:
            launch_ts = int(raw.get("createTime", 0) or raw.get("createdAt", 0) or 0)
        return {
            "holders": int(tp.get("holderCount", 0) or 0),
            "price": float(tp.get("price", 0) or 0),
            "totalSupply": int(raw.get("totalAmount", 0) or 0),
            "socialCount": len(social_links),
            "socialLinks": social_links,
            "descr": raw.get("descr", ""),
            "name": raw.get("name", ""),
            "shortName": raw.get("shortName", ""),
            "progress": float(tp.get("progress", 0) or raw.get("progress", 0) or 0),
            "day1Vol": float(tp.get("day1Vol", 0) or raw.get("day1Vol", 0) or 0),
            "liquidity": float(tp.get("liquidity", 0) or 0),
            "raisedAmount": float(tp.get("raisedAmount", 0) or raw.get("raisedAmount", 0) or 0),
            "launchTime": launch_ts,
        }
    except Exception as e:
        log.debug("fm_detail [%s]: %s", address[:20], e)
        return None


def fm_ticker_prices() -> dict[str, float]:
    _ensure_sessions()
    prices: dict[str, float] = {}
    try:
        r = _fm_session.post(FM_TICKER, json={}, timeout=20)
        r.raise_for_status()
        d = r.json()
        if d.get("code") == 0:
            for t in d.get("data", []):
                sym = t.get("symbol", "")
                if sym.endswith("USDT"):
                    base = sym[:-4].upper()
                    try:
                        prices[base] = float(t["price"])
                    except (ValueError, TypeError, KeyError):
                        pass
    except Exception as e:
        log.error("fm_ticker: %s", e)
    return prices


# ===================================================================
#  BSC RPC — 链上事件发现
# ===================================================================
def _rpc_call(method: str, params: list) -> dict | None:
    """BSC RPC JSON-RPC 调用"""
    _ensure_sessions()
    try:
        r = _bsc_session.post(BSC_RPC, json={
            "jsonrpc": "2.0", "method": method, "params": params, "id": 1,
        }, timeout=30, headers={"Content-Type": "application/json"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("RPC 调用失败 [%s]: %s", method, e)
        return None


def _parse_token_from_log(log_entry: dict) -> dict | None:
    """
    从 eth_getLogs 返回的单条日志中解析代币信息。
    支持两种事件格式:
      旧版 (12 words): word[0]=creator, word[1]=token, word[6]=timestamp, word[8+]=name/symbol
      新版 (8 words):  word[0]=token, word[1]=creator, 无时间戳/名称 (需 detail API 补全)
    """
    try:
        data = log_entry["data"][2:]  # 去掉 0x 前缀
        if len(data) < 128:
            return None

        n_words = len(data) // 64

        # 根据 data 长度区分新旧版本
        if n_words >= 10:
            # 旧版 (12 words): word[0]=creator, word[1]=token
            token_addr = ("0x" + data[88:128]).lower()
            creator_addr = ("0x" + data[24:64]).lower()

            # 创建时间戳 — word[6] 位置
            create_ts = 0
            TS_MIN, TS_MAX = 1577808000, 1893456000
            for word_idx in (6, 5, 7, 4):
                try:
                    offset = word_idx * 64
                    if len(data) >= offset + 64:
                        val = int(data[offset:offset + 64], 16)
                        if TS_MIN <= val <= TS_MAX:
                            create_ts = val
                            break
                except Exception:
                    continue

            # 解码名称和符号
            name, symbol = "", ""
            try:
                if len(data) >= 576:
                    name_len = int(data[512:576], 16)  # word[8]
                    if 0 < name_len < 200:
                        name = bytes.fromhex(data[576:576 + name_len * 2]).decode("utf-8", errors="replace")
                    name_words = max(1, (name_len + 31) // 32)
                    sym_len_offset = (9 + name_words) * 64
                    if sym_len_offset + 64 <= len(data):
                        sym_len = int(data[sym_len_offset:sym_len_offset + 64], 16)
                        if 0 < sym_len < 100:
                            symbol = bytes.fromhex(
                                data[sym_len_offset + 64:sym_len_offset + 64 + sym_len * 2]
                            ).decode("utf-8", errors="replace")
            except Exception:
                pass

        elif n_words == 8:
            # 新版 (8 words): word[0]=token, word[1]=creator, 无时间戳/名称
            token_addr = ("0x" + data[24:64]).lower()
            creator_addr = ("0x" + data[88:128]).lower()
            create_ts = 0  # 新版事件无时间戳, 由区块时间戳补全
            name, symbol = "", ""  # 新版事件无名称, 由 detail API 补全

        else:
            return None

        # 只保留 four.meme 代币 (后缀 4444 或 ffff)
        if not token_addr.endswith("4444") and not token_addr.endswith("ffff"):
            return None

        return {
            "address": token_addr,
            "creator": creator_addr,
            "createdAt": create_ts * 1000 if create_ts else 0,  # 毫秒
            "name": name,
            "symbol": symbol,
            "block": int(log_entry["blockNumber"], 16),
        }
    except Exception:
        return None


def discover_on_chain(from_block: int) -> tuple[list[dict], int]:
    """
    链上发现: 通过 BSC RPC eth_getLogs 查询所有 four.meme 工厂合约的代币创建事件
    支持多版本合约 (FOUR_MEME_FACTORIES), 新增合约只需在常量区追加即可。
    返回: (新代币列表, 最新区块号)
    """
    # 获取最新区块号
    block_res = _rpc_call("eth_blockNumber", [])
    if not block_res or not block_res.get("result"):
        log.warning("获取最新区块号失败")
        return [], from_block
    latest_block = int(block_res["result"], 16)

    if from_block <= 0:
        # 首次运行: 只扫最近 15 分钟 (~300 blocks, BSC ~3s/block)
        from_block = latest_block - 300

    # 安全上限: 不超过 5000 blocks (~4h, GitHub Actions 可能延迟较大)
    if latest_block - from_block > 5000:
        log.warning("区块跨度过大 (%d), 截断到最近 5000 blocks", latest_block - from_block)
        from_block = latest_block - 5000

    log.info("链上扫描区块 %d ~ %d (%d blocks), 监听 %d 个工厂合约",
             from_block, latest_block, latest_block - from_block, len(FOUR_MEME_FACTORIES))

    tokens = []
    seen_addrs = set()  # 去重 (理论上不同合约不会创建同一代币, 但防御性编程)
    chunk = 10000
    current = from_block

    while current <= latest_block:
        end = min(current + chunk - 1, latest_block)

        # 对每个工厂合约 + 事件组合分别查询
        for factory_addr, event_topic in FOUR_MEME_FACTORIES:
            try:
                res = _rpc_call("eth_getLogs", [{
                    "address": factory_addr,
                    "fromBlock": hex(current),
                    "toBlock": hex(end),
                    "topics": [event_topic],
                }])

                if not res:
                    continue

                if res.get("error"):
                    err_msg = res["error"].get("message", "")
                    if "pruned" in err_msg:
                        continue
                    log.warning("RPC error (%s): %s", factory_addr[:10], err_msg)
                    continue

                for log_entry in (res.get("result") or []):
                    parsed = _parse_token_from_log(log_entry)
                    if parsed and parsed["address"] not in seen_addrs:
                        seen_addrs.add(parsed["address"])
                        tokens.append(parsed)

            except Exception as e:
                log.warning("链上扫描异常 (%s): %s", factory_addr[:10], e)

        current = end + 1
        time.sleep(0.1)

    log.info("链上发现 %d 个新代币 (来自 %d 个工厂合约)", len(tokens), len(FOUR_MEME_FACTORIES))

    # 回退: 对 createdAt=0 的代币, 用区块时间戳补全
    need_block_ts = [t for t in tokens if t.get("createdAt", 0) == 0]
    if need_block_ts:
        block_nums = list({t["block"] for t in need_block_ts})
        log.info("补全 %d 个代币的创建时间 (查 %d 个区块时间戳)", len(need_block_ts), len(block_nums))
        block_ts_map = {}
        for bn in block_nums:
            res = _rpc_call("eth_getBlockByNumber", [hex(bn), False])
            if res and res.get("result"):
                ts = int(res["result"].get("timestamp", "0x0"), 16)
                if ts > 0:
                    block_ts_map[bn] = ts
            time.sleep(0.05)
        now_sec = int(time.time())
        for t in need_block_ts:
            ts = block_ts_map.get(t["block"], 0)
            if ts > 0:
                t["createdAt"] = ts * 1000
                log.info("  补全 %s 创建时间: %d", t.get("name") or t["address"][:16], ts)
            else:
                # 兜底: 区块时间戳也查不到, 用当前时间代替 (至少不会算成 1970 年)
                t["createdAt"] = now_sec * 1000
                log.warning("  补全失败 %s, 使用当前时间兜底", t.get("name") or t["address"][:16])

    return tokens, latest_block


# ===================================================================
#  RPC 查持币地址数 — 通过 eth_getLogs 查 ERC-20 Transfer 事件
#  通过追踪每个地址的净余额 (转入-转出), 只统计余额>0的地址
# ===================================================================
def rpc_holder_counts(token_infos: list[dict],
                      return_logs: bool = False
                      ) -> dict[str, int] | tuple[dict[str, int], dict[str, list]]:
    """
    RPC 查持币地址数: 通过 eth_getLogs 查 ERC-20 Transfer 事件
    return_logs: True 时同时返回原始日志 (用于聪明钱匹配)
    返回: {address: holder_count} 或 ({address: holder_count}, {address: [logs]})
    """
    result = {}
    logs_map: dict[str, list] = {}
    if not token_infos:
        return (result, logs_map) if return_logs else result

    # 获取当前区块号
    latest_block = 0
    try:
        block_res = _rpc_call("eth_blockNumber", [])
        if block_res and block_res.get("result"):
            latest_block = int(block_res["result"], 16)
    except Exception:
        pass

    def _query_one(info: dict) -> tuple[str, int | None, list]:
        addr = info["address"]
        block = info.get("block", 0)
        try:
            # 确定起始区块: 优先用代币创建区块, 否则用最大范围 (50000块 ≈ 42h)
            if block > 0:
                from_block = max(0, block - 1)
            else:
                from_block = max(0, latest_block - 50000)

            # RPC 限制最大 50000 块, 超过时分段查询
            all_logs = []
            chunk_size = 50000
            current = from_block
            while current <= latest_block:
                end = min(current + chunk_size - 1, latest_block)
                res = _rpc_call("eth_getLogs", [{
                    "address": addr,
                    "fromBlock": hex(current),
                    "toBlock": hex(end),
                    "topics": [ERC20_TRANSFER_TOPIC],
                }])
                if not res or res.get("error"):
                    err_msg = (res or {}).get("error", {}).get("message", "")
                    if "exceed" in err_msg or "range" in err_msg:
                        chunk_size = chunk_size // 2
                        if chunk_size < 1000:
                            break
                        continue
                    break
                all_logs.extend(res.get("result") or [])
                current = end + 1

            if not all_logs:
                return addr, None, []

            # 追踪每个地址的净余额
            balances: dict[str, int] = {}
            for log_entry in all_logs:
                topics = log_entry.get("topics", [])
                if len(topics) < 3:
                    continue
                from_addr = ("0x" + topics[1][26:]).lower()
                to_addr = ("0x" + topics[2][26:]).lower()
                data = log_entry.get("data", "0x0")
                value = int(data, 16) if data and data != "0x" else 0
                if from_addr not in BURN_ADDRESSES:
                    balances[from_addr] = balances.get(from_addr, 0) - value
                if to_addr not in BURN_ADDRESSES:
                    balances[to_addr] = balances.get(to_addr, 0) + value

            exclude = {ZERO_ADDRESS, DEAD_ADDRESS, addr.lower(),
                       FOUR_MEME_CONTRACT.lower()}
            holder_count = sum(1 for a, bal in balances.items()
                               if bal > 0 and a not in exclude)
            return addr, holder_count if holder_count > 0 else None, all_logs

        except Exception:
            return addr, None, []

    # 并发查询 (10 线程)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_query_one, info) for info in token_infos]
        for f in as_completed(futures):
            addr, count, raw_logs = f.result()
            if count is not None:
                result[addr] = count
            if return_logs and raw_logs:
                logs_map[addr] = raw_logs

    log.info("RPC 查到 %d/%d 个代币持币数", len(result), len(token_infos))
    return (result, logs_map) if return_logs else result


# ===================================================================
#  BSCScan API — 统一请求封装 (Etherscan V2, chainid=56)
# ===================================================================
BSCSCAN_TIMEOUT = 15
BSCSCAN_MAX_RETRIES = 2


def _bscscan_get(params: dict, api_key: str,
                 timeout: int = BSCSCAN_TIMEOUT) -> dict | None:
    """BSCScan API 统一 GET 请求封装 (Etherscan V2 API, chainid=56)"""
    if not api_key:
        return None
    _ensure_sessions()
    params = {**params, "apikey": api_key, "chainid": "56"}
    for attempt in range(BSCSCAN_MAX_RETRIES + 1):
        try:
            r = _bsc_session.get(BSCSCAN_API, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = 3 * (attempt + 1)
                log.warning("BSCScan 429, 等待 %ds (%d/%d)",
                            wait, attempt + 1, BSCSCAN_MAX_RETRIES + 1)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.ReadTimeout:
            if attempt < BSCSCAN_MAX_RETRIES:
                log.debug("BSCScan 超时, 重试 (%d/%d)", attempt + 1, BSCSCAN_MAX_RETRIES)
                time.sleep(2)
                continue
            log.warning("BSCScan 超时, 已达最大重试次数")
        except Exception as e:
            log.debug("BSCScan 请求异常: %s", e)
            if attempt < BSCSCAN_MAX_RETRIES:
                time.sleep(2)
                continue
    return None


def bscscan_holder_count(token_address: str, api_key: str) -> int | None:
    """通过 BSCScan API 获取链上真实持仓地址数"""
    d = _bscscan_get({
        "module": "token", "action": "tokenholdercount",
        "contractaddress": token_address,
    }, api_key)
    if d and d.get("status") == "1" and d.get("result"):
        try:
            return int(d["result"])
        except (ValueError, TypeError):
            pass
    return None


def bscscan_holder_counts_batch(addresses: list[str], api_key: str) -> dict[str, int]:
    """批量查询持币地址数"""
    result = {}
    if not api_key:
        return result
    for addr in addresses:
        count = bscscan_holder_count(addr, api_key)
        if count is not None:
            result[addr] = count
        time.sleep(0.2)
    log.info("BSCScan 查到 %d/%d 个代币持币数", len(result), len(addresses))
    return result


def bscscan_get_token_creator(token_address: str, api_key: str) -> str | None:
    """通过 BSCScan API 获取代币合约创建者地址"""
    d = _bscscan_get({
        "module": "contract", "action": "getcontractcreation",
        "contractaddresses": token_address,
    }, api_key)
    if d and d.get("status") == "1" and d.get("result"):
        return d["result"][0].get("contractCreator", "").lower()
    return None


def bscscan_get_token_transfers(token_address: str, address: str,
                                api_key: str, page: int = 1,
                                offset: int = 100) -> list[dict]:
    """通过 BSCScan API 获取指定地址的代币转账记录"""
    d = _bscscan_get({
        "module": "account", "action": "tokentx",
        "contractaddress": token_address,
        "address": address,
        "page": page, "offset": offset,
        "sort": "desc",
    }, api_key)
    if d and d.get("status") == "1" and d.get("result"):
        return d["result"]
    return []


def bscscan_top_holders(token_address: str, api_key: str,
                        offset: int = 50) -> list[dict]:
    """通过 BSCScan API 获取 Top Holders 列表"""
    d = _bscscan_get({
        "module": "token", "action": "tokenholderlist",
        "contractaddress": token_address,
        "page": 1, "offset": offset,
    }, api_key)
    if d and d.get("status") == "1" and d.get("result"):
        return d["result"]
    return []


# ===================================================================
#  链上行为分析 — 开发者行为
# ===================================================================
def analyze_developer_behavior(token_address: str, creator: str,
                               api_key: str) -> dict:
    """
    分析开发者链上行为
    改进: DEX Router 精确匹配, LP token mint/burn 检测, 销毁地址排除
    """
    result = {
        "has_sell": False, "has_buy": False,
        "has_lp_add": False, "has_lp_remove": False,
        "sell_pct": 0.0, "details": [],
        "bonus": 0, "exclude": False,
    }
    if not creator or not api_key:
        return result

    token_lower = token_address.lower()
    creator_lower = creator.lower()

    transfers = bscscan_get_token_transfers(token_address, creator, api_key)
    if not transfers:
        return result

    total_in = 0
    total_out = 0
    buy_count = 0
    sell_count = 0
    lp_add_count = 0
    lp_remove_count = 0

    for tx in transfers:
        from_addr = (tx.get("from") or "").lower()
        to_addr = (tx.get("to") or "").lower()
        value = int(tx.get("value", 0) or 0)
        contract_addr = (tx.get("contractAddress") or "").lower()

        if value <= 0:
            continue

        if contract_addr == token_lower:
            if to_addr == creator_lower:
                total_in += value
                if from_addr in KNOWN_DEX_ROUTERS or from_addr == ZERO_ADDRESS:
                    buy_count += 1
            elif from_addr == creator_lower:
                # 转到零地址/死地址 = 销毁, 不算卖出
                if to_addr in BURN_ADDRESSES:
                    continue
                total_out += value
                if to_addr in KNOWN_DEX_ROUTERS:
                    sell_count += 1
                else:
                    # 转到其他地址也算减仓
                    sell_count += 1
        else:
            # 非该代币的转账 — 检查是否是 LP token 操作
            token_name_lower = (tx.get("tokenName") or "").lower()
            if "lp" in token_name_lower or "pancake" in token_name_lower:
                if from_addr == ZERO_ADDRESS and to_addr == creator_lower:
                    lp_add_count += 1
                elif from_addr == creator_lower and to_addr == ZERO_ADDRESS:
                    lp_remove_count += 1

    if total_in > 0:
        result["sell_pct"] = min(100.0, (total_out / total_in) * 100)

    if sell_count > 0:
        result["has_sell"] = True
        if result["sell_pct"] >= 90:
            result["details"].append(f"开发者清仓 (卖出{result['sell_pct']:.0f}%)")
        else:
            result["details"].append(f"开发者减仓 (卖出{result['sell_pct']:.0f}%)")
        result["exclude"] = True

    if buy_count > 0:
        result["has_buy"] = True
        result["details"].append(f"开发者加仓 ({buy_count}笔)")
        result["bonus"] += 1

    if lp_add_count > 0:
        result["has_lp_add"] = True
        result["details"].append(f"开发者加池子 ({lp_add_count}笔)")
        result["bonus"] += 1

    if lp_remove_count > 0:
        result["has_lp_remove"] = True
        result["details"].append(f"开发者撤池子 ({lp_remove_count}笔)")
        result["exclude"] = True

    return result


# ===================================================================
#  币安 Web3 聪明钱信号
# ===================================================================
# 缓存: {ts: float, signals: dict[str, dict]}
_binance_signal_cache: dict = {"ts": 0, "signals": {}}
BINANCE_SIGNAL_CACHE_TTL = 300  # 5 分钟缓存 (信号更新频率不高)


def fetch_binance_smart_signals() -> dict[str, dict]:
    """
    从币安 Web3 钱包 API 获取 BSC 聪明钱信号
    首次请求超时 5 秒快速失败, 避免 API 不可用时阻塞
    """
    global _binance_signal_cache
    now = time.time()
    if now - _binance_signal_cache["ts"] < BINANCE_SIGNAL_CACHE_TTL and _binance_signal_cache["signals"]:
        return _binance_signal_cache["signals"]

    _ensure_sessions()
    signal_map: dict[str, dict] = {}

    def _process_items(items: list):
        for item in items:
            addr = (item.get("contractAddress") or "").lower()
            if not addr or len(addr) != 42:
                continue
            tag_events = []
            for events in ((item.get("tokenTag") or {}).get("Sensitive Events") or []):
                tag_name = events.get("tagName", "")
                if tag_name:
                    tag_events.append(tag_name)
            signal = {
                "direction": item.get("direction", ""),
                "smartMoneyCount": item.get("smartMoneyCount", 0),
                "exitRate": item.get("exitRate", 0),
                "maxGain": item.get("maxGain", "0"),
                "status": item.get("status", ""),
                "alertPrice": item.get("alertPrice", "0"),
                "currentPrice": item.get("currentPrice", "0"),
                "totalTokenValue": item.get("totalTokenValue", "0"),
                "signalTriggerTime": item.get("signalTriggerTime", 0),
                "ticker": item.get("ticker", ""),
                "tagEvents": tag_events,
            }
            if addr not in signal_map or signal["smartMoneyCount"] > signal_map[addr]["smartMoneyCount"]:
                signal_map[addr] = signal

    try:
        # 第一页用短超时探测可用性
        resp = _bn_session.post(
            BINANCE_SMART_SIGNAL,
            json={"smartSignalType": "", "page": 1, "pageSize": 100, "chainId": "56"},
            timeout=5,
        )
        if resp.status_code != 200:
            log.info("币安聪明钱信号: 不可用 (status=%d), 跳过", resp.status_code)
            _binance_signal_cache = {"ts": now, "signals": signal_map}
            return signal_map

        data = resp.json()
        if data.get("code") != "000000" or not data.get("data"):
            _binance_signal_cache = {"ts": now, "signals": signal_map}
            return signal_map

        first_items = data["data"]
        _process_items(first_items)

        # 继续拉后续页
        if len(first_items) >= 100:
            for page in range(2, 4):
                try:
                    r = _bn_session.post(
                        BINANCE_SMART_SIGNAL,
                        json={"smartSignalType": "", "page": page, "pageSize": 100, "chainId": "56"},
                        timeout=10,
                    )
                    if r.status_code != 200:
                        break
                    d = r.json()
                    if d.get("code") != "000000" or not d.get("data"):
                        break
                    _process_items(d["data"])
                    if len(d["data"]) < 100:
                        break
                    time.sleep(0.3)
                except Exception:
                    break

        if signal_map:
            log.info("币安聪明钱信号: 获取 %d 个 BSC 代币信号", len(signal_map))

    except Exception as e:
        log.info("币安聪明钱信号: 不可用: %s", e)

    _binance_signal_cache = {"ts": now, "signals": signal_map}
    return signal_map


def fetch_binance_token_dynamic(addresses: list[str]) -> dict[str, dict]:
    """
    从币安 Web3 Token Dynamic Data API 批量获取代币动态数据
    连续 3 次失败则判定 API 不可用, 快速放弃
    """
    _ensure_sessions()
    result: dict[str, dict] = {}
    if not addresses:
        return result

    consec_fails = 0
    for i, addr in enumerate(addresses):
        if consec_fails >= 3:
            log.info("币安动态: 连续失败 %d 次, 跳过剩余 %d 个", consec_fails, len(addresses) - i)
            break

        try:
            resp = _bn_session.get(
                BINANCE_TOKEN_DYNAMIC,
                params={"chainId": "56", "contractAddress": addr},
                timeout=5,
            )
            if resp.status_code != 200:
                consec_fails += 1
                continue

            body = resp.json()
            if body.get("code") != "000000" or not body.get("data"):
                consec_fails += 1
                continue

            consec_fails = 0
            d = body["data"]
            result[addr.lower()] = {
                "price": float(d.get("price") or 0),
                "holders": int(d.get("holders") or 0),
                "liquidity": float(d.get("liquidity") or 0),
                "marketCap": float(d.get("marketCap") or 0),
                "volume24h": float(d.get("volume24h") or 0),
                "volume1h": float(d.get("volume1h") or 0),
                "percentChange1h": float(d.get("percentChange1h") or 0),
                "percentChange24h": float(d.get("percentChange24h") or 0),
                "top10Pct": float(d.get("top10HoldersPercentage") or 0),
                "devHoldPct": float(d.get("devHoldingPercent") or 0),
                "smartMoneyHolders": int(d.get("smartMoneyHolders") or 0),
                "smartMoneyHoldPct": float(d.get("smartMoneyHoldingPercent") or 0),
                "kolHolders": int(d.get("kolHolders") or 0),
                "kolHoldPct": float(d.get("kolHoldingPercent") or 0),
                "proHolders": int(d.get("proHolders") or 0),
                "proHoldPct": float(d.get("proHoldingPercent") or 0),
            }
        except Exception:
            consec_fails += 1

        if i < len(addresses) - 1:
            time.sleep(0.25)

    if result:
        log.info("币安代币动态: 获取 %d/%d 个代币数据", len(result), len(addresses))

    return result


# ===================================================================
#  GMGN 聪明钱地址发现 + 持久化
# ===================================================================
def _load_smart_money_file() -> dict:
    """加载聪明钱地址文件, 返回 {address: {tags, firstSeen, lastSeen}}"""
    try:
        if SMART_MONEY_FILE.exists():
            with open(SMART_MONEY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_smart_money_file(data: dict):
    """保存聪明钱地址文件"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SMART_MONEY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_gmgn_smart_money() -> set[str]:
    """
    从 GMGN API 获取 BSC 聪明钱地址, 合并到本地文件
    返回当前所有已知聪明钱地址集合
    """
    _ensure_sessions()
    sm_data = _load_smart_money_file()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    new_count = 0

    try:
        ts = int(time.time())
        cid = str(uuid.uuid4())
        url = (f"{GMGN_API}/v1/user/smartmoney"
               f"?chain=bsc&limit=100&timestamp={ts}&client_id={cid}")
        headers = {"X-APIKEY": GMGN_API_KEY, "Content-Type": "application/json"}

        resp = _bn_session.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            log.info("GMGN 聪明钱: 不可用 (status=%d)", resp.status_code)
            return set(sm_data.keys()) - KNOWN_EXCLUDE_ADDRESSES

        data = resp.json()
        items = (data.get("data") or {}).get("list") or []

        for item in items:
            addr = (item.get("maker") or "").lower()
            if not addr or len(addr) != 42 or addr in KNOWN_EXCLUDE_ADDRESSES:
                continue
            tags = (item.get("maker_info") or {}).get("tags") or []
            if addr in sm_data:
                sm_data[addr]["lastSeen"] = now_str
                existing_tags = set(sm_data[addr].get("tags", []))
                existing_tags.update(tags)
                sm_data[addr]["tags"] = list(existing_tags)
            else:
                sm_data[addr] = {
                    "tags": tags,
                    "firstSeen": now_str,
                    "lastSeen": now_str,
                }
                new_count += 1

        if new_count > 0:
            log.info("GMGN 聪明钱: 新增 %d 个地址 (累计 %d 个)", new_count, len(sm_data))
        else:
            log.info("GMGN 聪明钱: 无新增 (累计 %d 个)", len(sm_data))

        _save_smart_money_file(sm_data)

    except Exception as e:
        log.info("GMGN 聪明钱: 获取失败: %s", e)

    return set(sm_data.keys()) - KNOWN_EXCLUDE_ADDRESSES


def match_smart_money_in_transfers(token_address: str,
                                   smart_addresses: set[str],
                                   rpc_logs: list[dict]) -> dict:
    """
    在已有的 RPC Transfer 日志中匹配聪明钱地址
    返回 {has_buy, has_sell, buy_count, sell_count, details, bonus}
    """
    result = {"has_buy": False, "has_sell": False, "buy_count": 0,
              "sell_count": 0, "details": [], "bonus": 0}
    if not smart_addresses or not rpc_logs:
        return result

    buyers = set()
    sellers = set()

    for log_entry in rpc_logs:
        topics = log_entry.get("topics", [])
        if len(topics) < 3:
            continue
        from_addr = ("0x" + topics[1][26:]).lower()
        to_addr = ("0x" + topics[2][26:]).lower()

        if to_addr in smart_addresses:
            if from_addr in KNOWN_DEX_ROUTERS or from_addr == ZERO_ADDRESS:
                buyers.add(to_addr)

        if from_addr in smart_addresses:
            if to_addr in KNOWN_DEX_ROUTERS:
                sellers.add(from_addr)

    if buyers:
        result["has_buy"] = True
        result["buy_count"] = len(buyers)
        result["details"].append(f"聪明钱加仓 ({len(buyers)}个地址)")
        result["bonus"] = len(buyers)
    if sellers:
        result["has_sell"] = True
        result["sell_count"] = len(sellers)
        result["details"].append(f"聪明钱减仓 ({len(sellers)}个地址)")

    return result


# ===================================================================
#  钱包分析入口 — 批量分析开发者 + 币安信号 + 聪明钱
# ===================================================================
def batch_wallet_analysis(tokens: list[dict],
                          api_key: str,
                          binance_signals: dict[str, dict] | None = None,
                          smart_addresses: set[str] | None = None,
                          rpc_logs_map: dict[str, list] | None = None) -> dict[str, dict]:
    """
    批量分析钱包行为 (并发执行)
    返回 {address: {excluded, excludeReason, signals, bonus, details}}
    """
    result_map = {}
    if not api_key:
        return result_map

    bn_signals = binance_signals or {}
    sm_addrs = smart_addresses or set()
    logs_map = rpc_logs_map or {}

    def _analyze_one(t: dict) -> tuple[str, dict]:
        addr = t.get("address", "")
        creator = t.get("creator", "")

        # 分析开发者行为
        dev = analyze_developer_behavior(addr, creator, api_key)

        # 合并结果
        all_details = list(dev["details"])
        total_bonus = dev["bonus"]
        signals = []
        if dev["has_buy"]:
            signals.append("开发者加仓")
        if dev["has_lp_add"]:
            signals.append("开发者加池子")

        # 合并币安聪明钱信号
        bn = bn_signals.get(addr.lower())
        if bn:
            direction = bn.get("direction", "")
            sm_count = bn.get("smartMoneyCount", 0)
            status = bn.get("status", "")
            max_gain = bn.get("maxGain", "0")
            exit_rate = bn.get("exitRate", 0)
            tag_events = bn.get("tagEvents", [])

            # 信号描述
            if direction == "buy":
                detail_str = f"币安聪明钱买入 ({sm_count}个地址"
                if status == "active":
                    detail_str += ", 活跃"
                if max_gain and float(max_gain) > 0:
                    detail_str += f", 最高涨{float(max_gain):.1f}%"
                detail_str += ")"
                all_details.append(detail_str)
                signals.append("币安聪明钱买入")
                total_bonus += min(sm_count, 3)  # 最多加 3 分
            elif direction == "sell":
                detail_str = f"币安聪明钱卖出 ({sm_count}个地址, 退出率{exit_rate}%)"
                all_details.append(detail_str)
                signals.append("币安聪明钱卖出")

            # tokenTag 敏感事件 (鲸鱼买卖等)
            for evt in tag_events:
                if evt not in all_details:
                    all_details.append(evt)
                    if "Add" in evt or "Buy" in evt:
                        total_bonus += 1
                    elif "Reduce" in evt or "Sell" in evt:
                        signals.append(evt)

        # 币安动态数据: 开发者持仓变化 + 聪明钱/KOL/专业投资者持仓
        dev_pct = t.get("devHoldPct", -1)
        prev_dev_pct = t.get("prevDevHoldPct", -1)
        sm_holders_bn = t.get("smartMoneyHolders", 0)
        kol_holders = t.get("kolHolders", 0)
        pro_holders = t.get("proHolders", 0)
        bn_dev_exclude = False
        bn_dev_exclude_reason = ""

        # 开发者持仓变化检测 (通过两轮扫描对比)
        if prev_dev_pct >= 0 and dev_pct >= 0:
            if prev_dev_pct > 0 and dev_pct == 0:
                all_details.append(f"开发者清仓 (持仓{prev_dev_pct:.2f}%→0%)")
                bn_dev_exclude = True
                bn_dev_exclude_reason = f"开发者清仓 (币安: {prev_dev_pct:.2f}%→0%)"
            elif dev_pct < prev_dev_pct * 0.5 and prev_dev_pct > 1:
                all_details.append(f"开发者大幅减仓 (持仓{prev_dev_pct:.2f}%→{dev_pct:.2f}%)")
            elif dev_pct > prev_dev_pct * 1.5 and dev_pct > 0.1:
                all_details.append(f"开发者加仓 (持仓{prev_dev_pct:.2f}%→{dev_pct:.2f}%)")
                if "开发者加仓" not in signals:
                    signals.append("开发者加仓")
                total_bonus += 1

        # 币安标注的聪明钱/KOL/专业投资者持仓
        if sm_holders_bn > 0:
            all_details.append(f"币安聪明钱持仓 ({sm_holders_bn}个地址)")
            if "币安聪明钱买入" not in signals:
                total_bonus += 1
        if kol_holders > 0:
            all_details.append(f"KOL持仓 ({kol_holders}个)")
            total_bonus += 1
        if pro_holders > 0:
            all_details.append(f"专业投资者持仓 ({pro_holders}个)")
            total_bonus += 1

        # GMGN 聪明钱链上匹配 (用淘汰阶段已有的 RPC Transfer 日志)
        if sm_addrs and logs_map:
            rpc_logs = logs_map.get(addr, [])
            sm = match_smart_money_in_transfers(addr, sm_addrs, rpc_logs)
            if sm["has_buy"]:
                all_details.extend(sm["details"])
                signals.append("聪明钱加仓")
                total_bonus += sm["bonus"]
            if sm["has_sell"]:
                all_details.extend(sm["details"])

        excluded = False
        exclude_reason = ""
        if dev["exclude"]:
            excluded = True
            exclude_reason = ", ".join(dev["details"])
        if bn_dev_exclude and not dev["exclude"]:
            # 币安数据检测到开发者清仓, 但链上分析没检测到 → 补充排除
            excluded = True
            if exclude_reason:
                exclude_reason += ", "
            exclude_reason += bn_dev_exclude_reason

        return addr, {
            "excluded": excluded,
            "excludeReason": exclude_reason,
            "signals": signals,
            "bonus": total_bonus,
            "details": all_details,
        }

    # 并发分析 (8 线程, BSCScan 限流 ~5 req/s)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_analyze_one, t) for t in tokens]
        for f in as_completed(futures):
            try:
                addr, wa = f.result()
                result_map[addr] = wa
            except Exception:
                pass

    return result_map


# ===================================================================
#  DexScreener API (主要价格源, ~300 req/min)
# ===================================================================
def ds_get_pairs(token_address: str) -> list[dict] | None:
    _ensure_sessions()
    url = f"{DS_BASE}/tokens/v1/bsc/{token_address}"
    try:
        r = _gt_session.get(url, timeout=10, headers=DS_HEADERS)
        if r.status_code == 429:
            time.sleep(2)
            r = _gt_session.get(url, timeout=10, headers=DS_HEADERS)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("pairs") or data.get("data") or []
    except Exception as e:
        log.debug("ds_get_pairs [%s]: %s", token_address[:20], e)
        return None


def ds_batch_prices(addresses: list[str]) -> dict[str, dict]:
    """DexScreener 批量查价格+流动性 (最多 10 个地址/请求, 避免 URL 过长被拒)"""
    _ensure_sessions()
    result = {}
    batch_size = 10

    for i in range(0, len(addresses), batch_size):
        batch = addresses[i:i + batch_size]
        try:
            url = f"{DS_BASE}/tokens/v1/bsc/{','.join(batch)}"
            r = _gt_session.get(url, timeout=15, headers=DS_HEADERS)
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            data = r.json()
            pairs = data if isinstance(data, list) else (data.get("pairs") or data.get("data") or [])
            for p in pairs:
                if not p.get("baseToken"):
                    continue
                addr = p["baseToken"]["address"].lower()
                if addr in result:
                    continue
                result[addr] = {
                    "price": float(p.get("priceUsd") or 0),
                    "liquidity": float((p.get("liquidity") or {}).get("usd") or 0),
                    "volume24h": float((p.get("volume") or {}).get("h24") or 0),
                    "name": p["baseToken"].get("name", ""),
                    "symbol": p["baseToken"].get("symbol", ""),
                }
        except Exception as e:
            log.warning("DS 批量查价失败: %s", e)
        if i + batch_size < len(addresses):
            time.sleep(0.3)

    return result


# ===================================================================
#  GeckoTerminal API (备选K线, ~30 req/min)
# ===================================================================
def _gt_request(url: str, max_retries: int = 3) -> dict | None:
    global _gt_rate_delay
    _ensure_sessions()
    for attempt in range(max_retries):
        try:
            r = _gt_session.get(url, timeout=15)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                _gt_rate_delay = min(5.0, _gt_rate_delay + 1.0)
                log.warning("GT 429, 等待 %ds (%d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            r.raise_for_status()
            _gt_rate_delay = max(0.5, _gt_rate_delay - 0.2)
            return r.json()
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(3)
    return None


def gt_ohlcv_direct(token_address: str, limit: int = 72) -> list[list]:
    """直接用 tokenAddress 当 poolAddress 拿 1h K线"""
    url = f"{GT_BASE}/networks/bsc/pools/{token_address}/ohlcv/hour?aggregate=1&limit={limit}"
    data = _gt_request(url)
    if not data:
        return []
    return (data.get("data", {}).get("attributes", {}).get("ohlcv_list", []))


def gt_ohlcv_15min(token_address: str, limit: int = 48) -> list[list]:
    """拿 15 分钟 K线 (用于币龄<1h的底价计算)"""
    url = f"{GT_BASE}/networks/bsc/pools/{token_address}/ohlcv/minute?aggregate=15&limit={limit}"
    data = _gt_request(url)
    if not data:
        return []
    return (data.get("data", {}).get("attributes", {}).get("ohlcv_list", []))


def calc_all_time_high(candles: list[list]) -> float | None:
    if not candles:
        return None
    return max(float(c[2]) for c in candles)


def calc_max_price_first_n_hours(candles: list[list], create_ts_sec: int,
                                 hours: int = 2) -> float | None:
    """计算前 N 小时的最高价"""
    if not candles:
        return None
    cutoff = create_ts_sec + hours * 3600
    max_high, found = 0.0, False
    for c in candles:
        ts = int(c[0])
        if ts > cutoff or ts < create_ts_sec - 3600:
            continue
        high = float(c[2])
        if high > max_high:
            max_high = high
        found = True
    return max_high if found else None


def calc_min_price_exclude_first(candles: list[list], create_ts_sec: int) -> float | None:
    """计算排除第1根K线后的最低价 (从第二根K线开始统计)"""
    if not candles or len(candles) < 2:
        return None
    sorted_c = sorted(candles, key=lambda c: int(c[0]))
    first_ts = int(sorted_c[0][0])
    min_low, found = float("inf"), False
    for c in sorted_c:
        if int(c[0]) == first_ts:
            continue
        low = float(c[3])
        if low > 0 and low < min_low:
            min_low = low
            found = True
    return min_low if found else None


def calc_min_price_all(candles: list[list]) -> float | None:
    """计算全部K线的最低价"""
    if not candles:
        return None
    min_low, found = float("inf"), False
    for c in candles:
        low = float(c[3])
        if low > 0 and low < min_low:
            min_low = low
            found = True
    return min_low if found else None


# ===================================================================
#  Step 2: 入场筛 — four.meme detail API (返回 admitted + rejected)
# ===================================================================
def admission_filter(new_tokens: list[dict],
                     existing_addrs: set[str]) -> tuple[list[dict], list[dict]]:
    """
    入场筛: 对新发现的代币调 detail API, 淘汰无社交/总量≠10亿/币龄过大
    同时用 detail API 的 launchTime 修正链上解析可能不准的 createdAt
    返回: (admitted, rejected)
      admitted: [{"token": ..., "detail": ...}]
      rejected: [{"token": ..., "detail": ..., "reason": "..."}]
    """
    admitted = []
    rejected = []
    # 过滤已在队列或已淘汰的
    fresh = [t for t in new_tokens if t["address"] not in existing_addrs]
    if not fresh:
        return admitted, rejected

    log.info("入场筛: 对 %d 个新代币调 detail API...", len(fresh))

    batch_size = 5
    now_ms = int(time.time() * 1000)
    max_age_ms = MAX_AGE_HOURS * 3600 * 1000
    for i in range(0, len(fresh), batch_size):
        batch = fresh[i:i + batch_size]
        for t in batch:
            detail = fm_detail(t["address"])
            if not detail:
                rejected.append({"token": t, "detail": None, "reason": "detail API 无数据"})
                continue

            # 用 detail API 的 launchTime 修正 createdAt (链上解析可能不准)
            if detail.get("launchTime") and detail["launchTime"] > 0:
                t["createdAt"] = detail["launchTime"]

            # 入场条件: 币龄不超过 MAX_AGE_HOURS
            token_age_ms = now_ms - t.get("createdAt", 0)
            if t.get("createdAt", 0) <= 0 or token_age_ms > max_age_ms:
                age_desc = f"币龄{token_age_ms / 3600000:.1f}h" if t.get("createdAt", 0) > 0 else "创建时间未知"
                rejected.append({"token": t, "detail": detail, "reason": f"币龄过大 ({age_desc})"})
                continue

            # 入场条件: 社交 ≥ 1, 总供应量 = 10亿
            reasons = []
            if detail["socialCount"] < MIN_SOCIAL_COUNT:
                reasons.append("无社交媒体")
            if detail["totalSupply"] != TOTAL_SUPPLY:
                reasons.append(f"总量≠10亿 ({detail['totalSupply']})")
            if reasons:
                rejected.append({"token": t, "detail": detail, "reason": ", ".join(reasons)})
                continue
            admitted.append({"token": t, "detail": detail})
            time.sleep(0.2)

    log.info("入场筛: 通过 %d/%d (淘汰 %d: 无社交/总量不符)",
             len(admitted), len(fresh), len(rejected))
    return admitted, rejected


# ===================================================================
#  Step 3: 淘汰检查 — DexScreener + four.meme detail + BSCScan
# ===================================================================
def elimination_check(queue: list[dict], now_ms: int,
                      api_key: str) -> tuple[list[dict], list[dict]]:
    """
    淘汰检查: 对队列中代币定期检查, 永久淘汰弃盘币
    v6: 去掉 K线ATH修正、币安动态、RPC持币数, 只用 DexScreener + detail API
    返回: (survivors, eliminated)
    """
    survivors = []
    eliminated = []

    if not queue:
        return survivors, eliminated

    # 1. 币龄淘汰 (无需 API)
    max_age_ms = MAX_AGE_HOURS * 3600 * 1000
    age_filtered = []
    for t in queue:
        if now_ms - t.get("createdAt", 0) > max_age_ms:
            eliminated.append({**t, "eliminatedAt": now_ms,
                               "elimReason": f"币龄>{MAX_AGE_HOURS}h"})
        else:
            age_filtered.append(t)

    if eliminated:
        log.info("淘汰: 币龄超限 %d 个", len(eliminated))

    if not age_filtered:
        return survivors, eliminated

    # 1b. 本地预淘汰: 用已有数据快速剔除明显垃圾币 (零 API 调用)
    pre_filtered = []
    for t in age_filtered:
        age_hours = (now_ms - t.get("createdAt", 0)) / 3600000
        elim_reason = None

        if age_hours > ELIM_EARLY_AGE_MIN and t.get("peakHolders", 0) < ELIM_EARLY_PEAK_HOLDERS:
            elim_reason = f"币龄{age_hours:.1f}h 最高持币仅{t.get('peakHolders', 0)}"
        elif age_hours > ELIM_MID_AGE_HOURS and t.get("peakHolders", 0) < ELIM_MID_PEAK_HOLDERS:
            elim_reason = f"币龄{age_hours:.1f}h 最高持币仅{t.get('peakHolders', 0)}"

        if elim_reason:
            eliminated.append({**t, "eliminatedAt": now_ms, "elimReason": elim_reason})
        else:
            pre_filtered.append(t)

    pre_elim_count = len(age_filtered) - len(pre_filtered)
    if pre_elim_count > 0:
        log.info("淘汰: 本地预淘汰 %d 个 (持币数不足)", pre_elim_count)

    if not pre_filtered:
        return survivors, eliminated

    # 2. DexScreener + four.meme detail 并行查询 (v6: 去掉 RPC持币数/K线/币安动态)
    addrs = [t["address"] for t in pre_filtered]

    # 新入队代币 (本轮刚加入) 不需要再查 detail, 入场筛已经查过
    need_detail = [t for t in pre_filtered if now_ms - t.get("addedAt", 0) > 60000]

    log.info("淘汰检查: 并行查询 %d 个代币 (DS + detail(%d个))...",
             len(pre_filtered), len(need_detail))

    def _fetch_all_details():
        detail_map = {}
        for t in need_detail:
            detail = fm_detail(t["address"])
            if detail:
                detail_map[t["address"]] = detail
            time.sleep(0.2)
        return detail_map

    with ThreadPoolExecutor(max_workers=2) as pool:
        ds_future = pool.submit(ds_batch_prices, addrs)
        detail_future = pool.submit(_fetch_all_details)
        ds_data = ds_future.result()
        detail_map = detail_future.result()

    # 3. 逐个检查淘汰条件
    for t in pre_filtered:
        ds = ds_data.get(t["address"], {})
        detail = detail_map.get(t["address"])
        age_hours = (now_ms - t.get("createdAt", 0)) / 3600000

        # 更新动态数据 (DexScreener > detail > 队列缓存)
        current_price = (ds.get("price")
                         or (detail["price"] if detail else 0)
                         or t.get("price", 0))
        current_holders = ((detail["holders"] if detail else 0)
                           or t.get("holders", 0))
        current_liq = (ds.get("liquidity")
                       or t.get("liquidity", 0))
        current_progress = (detail["progress"] if detail else 0) or t.get("progress", 0)

        t["price"] = current_price
        t["holders"] = current_holders
        # 记录持币数历史 (只保留最近 5 轮)
        hist = t.get("holdersHistory", [])
        hist.append(current_holders)
        t["holdersHistory"] = hist[-5:]
        t["liquidity"] = current_liq
        t["progress"] = current_progress
        if detail:
            t["socialCount"] = detail["socialCount"]
            t["socialLinks"] = detail["socialLinks"]
            t["day1Vol"] = detail.get("day1Vol") or t.get("day1Vol", 0)
            t["raisedAmount"] = detail.get("raisedAmount") or t.get("raisedAmount", 0)
            # 用 detail API 的 launchTime 修正 createdAt
            if detail.get("launchTime") and detail["launchTime"] > 0:
                t["createdAt"] = detail["launchTime"]
                age_hours = (now_ms - t["createdAt"]) / 3600000
        if ds:
            t["name"] = ds.get("name") or t.get("name", "")
            t["symbol"] = ds.get("symbol") or t.get("symbol", "")

        # 更新峰值
        t["peakPrice"] = max(t.get("peakPrice", 0), current_price)
        t["peakHolders"] = max(t.get("peakHolders", 0), current_holders)
        t["peakLiquidity"] = max(t.get("peakLiquidity", 0), current_liq)

        # 记录价格历史 (只保留最近 5 轮, 与 holdersHistory 对齐)
        price_hist = t.get("priceHistory", [])
        price_hist.append(current_price)
        t["priceHistory"] = price_hist[-5:]

        # 连续下跌计数
        last_price = t.get("lastPrice", 0)
        if last_price > 0 and current_price < last_price:
            t["consecDrops"] = t.get("consecDrops", 0) + 1
        else:
            t["consecDrops"] = 0
        t["lastPrice"] = current_price

        # --- 淘汰条件 ---
        elim_reason = None

        # 1. 价格从峰值跌 90%+
        peak = t.get("peakPrice", 0)
        if peak > 0 and current_price > 0 and current_price < peak * (1 - ELIM_PRICE_DROP_PCT):
            elim_reason = (f"价格跌{(1 - current_price / peak) * 100:.0f}% "
                           f"(峰:{peak:.2e} 现:{current_price:.2e})")

        # 2. 持币数从 30+ 跌破 10
        if not elim_reason:
            if (t.get("peakHolders", 0) >= ELIM_HOLDERS_PEAK_MIN
                    and current_holders < ELIM_HOLDERS_FLOOR):
                elim_reason = f"持币数 {t.get('peakHolders', 0)}→{current_holders}"

        # 3. 无社交媒体
        if not elim_reason and detail and detail["socialCount"] < MIN_SOCIAL_COUNT:
            elim_reason = "无社交媒体"

        # 4. 流动性从 >$1k 跌破 $100
        if not elim_reason:
            if (t.get("peakLiquidity", 0) >= ELIM_LIQ_PEAK_MIN
                    and current_liq < ELIM_LIQ_FLOOR):
                elim_reason = f"流动性 ${t.get('peakLiquidity', 0):.0f}→${current_liq:.0f}"

        # 5. 进度 < 1% 且币龄 > 2h
        if not elim_reason:
            if age_hours > ELIM_PROGRESS_AGE_HOURS and current_progress < ELIM_PROGRESS_MIN:
                elim_reason = f"进度{current_progress * 100:.2f}% 币龄{age_hours:.1f}h"

        # 5b. 进度 < 5% 且币龄 > 4h
        if not elim_reason:
            if age_hours > ELIM_PROGRESS_AGE_HOURS_MID and current_progress < ELIM_PROGRESS_MIN_MID:
                elim_reason = f"进度{current_progress * 100:.2f}% 币龄{age_hours:.1f}h"

        # 6. 币龄>15min 最高持币数 < 3
        if not elim_reason:
            if age_hours > ELIM_EARLY_AGE_MIN and t.get("peakHolders", 0) < ELIM_EARLY_PEAK_HOLDERS:
                elim_reason = f"币龄{age_hours:.1f}h 最高持币仅{t.get('peakHolders', 0)}"

        # 7. 币龄>1h 最高持币数 < 5
        if not elim_reason:
            if age_hours > ELIM_MID_AGE_HOURS and t.get("peakHolders", 0) < ELIM_MID_PEAK_HOLDERS:
                elim_reason = f"币龄{age_hours:.1f}h 最高持币仅{t.get('peakHolders', 0)}"

        # 8. 修正后币龄 > 48h
        if not elim_reason:
            if age_hours > MAX_AGE_HOURS:
                elim_reason = f"币龄>{MAX_AGE_HOURS}h (修正后{age_hours:.1f}h)"

        if elim_reason:
            eliminated.append({**t, "eliminatedAt": now_ms, "elimReason": elim_reason})
        else:
            survivors.append(t)

    elim_count = len(eliminated) - (len(queue) - len(pre_filtered))
    if elim_count > 0:
        log.info("淘汰: 条件淘汰 %d 个", elim_count)
        for e in eliminated[-elim_count:]:
            log.info("  ✗ %s — %s", e.get("name") or e["address"][:16], e["elimReason"])

    return survivors, eliminated


# ===================================================================
#  Step 5: 精筛 — K线 + 价格比 + 钱包行为排除/加分
# ===================================================================
def quality_filter(candidates: list[dict], now_ms: int) -> list[dict]:
    """
    精筛: 两档条件 + 趋势确认
    快档 (币龄 ≤ 5min):
      - 持币地址数 ≥ 30
      - 当前价 < $0.000006
    慢档 (币龄 > 5min 且 ≤ 15min):
      - 持币地址数 ≥ 20
      - 当前价 < $0.000008
    通用趋势条件 (两档共用):
      - 持币地址数近 2 轮扫描递增 (不足 2 轮不通过)
      - 价格近 2 轮扫描递增 (不足 2 轮不通过)
    """
    results = []
    fast_max_ms = QUALITY_MAX_AGE_MIN * 60 * 1000
    slow_max_ms = QUALITY_SLOW_MAX_AGE_MIN * 60 * 1000

    for t in candidates:
        age_ms = now_ms - t.get("createdAt", 0)
        current_price = t.get("price", 0)
        holders = t.get("holders", 0)
        name = t.get("name") or t.get("address", "")[:16]

        if current_price <= 0:
            continue

        if age_ms <= fast_max_ms:
            # 快档: 币龄 ≤ 5 分钟
            if holders < QUALITY_MIN_HOLDERS:
                continue
            if current_price >= QUALITY_MAX_PRICE:
                continue
            tier = "快档"
        elif age_ms <= slow_max_ms:
            # 慢档: 币龄 > 5min 且 ≤ 15min
            if holders < QUALITY_SLOW_MIN_HOLDERS:
                continue
            if current_price >= QUALITY_SLOW_MAX_PRICE:
                continue
            tier = "慢档"
        else:
            # 币龄 > 15 分钟, 不通过
            continue

        # 趋势条件: 持币地址数近 2 轮递增 (不足 2 轮不通过)
        h_hist = t.get("holdersHistory", [])
        if len(h_hist) < 2 or h_hist[-1] <= h_hist[-2]:
            continue

        # 趋势条件: 价格近 2 轮递增 (不足 2 轮不通过)
        p_hist = t.get("priceHistory", [])
        if len(p_hist) < 2 or p_hist[-1] <= p_hist[-2]:
            continue

        results.append(t)
        log.info("精筛: ✓ %s [%s] — 价格 %.3e, 持币 %d, 币龄 %.1fmin",
                 name, tier, current_price, holders, age_ms / 60000)

    return results


# ===================================================================
#  数据文件清理 — 删除 7 天前的 JSON 文件
# ===================================================================
def cleanup_old_data(max_age_days: int = 7):
    """删除 data/ 目录下超过 max_age_days 天的 JSON 文件 (排除 queue.json)"""
    now = time.time()
    cutoff = now - max_age_days * 86400
    count = 0
    for f in DATA_DIR.glob("*.json"):
        if f.name == "queue.json":
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                count += 1
        except Exception:
            pass
    if count > 0:
        log.info("清理: 删除 %d 个过期数据文件 (>%d天)", count, max_age_days)


# ===================================================================
#  主扫描流程
# ===================================================================
def main():
    log.info("=" * 50)
    log.info("🚀 BSC Token Scanner v6 极速版 (Python, GitHub Actions)")
    log.info("=" * 50)

    # 加载配置
    cfg = load_config()
    bscscan_key = cfg.get("bscscanApiKey", "")

    # 确保 data 目录存在
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 初始化 HTTP sessions
    global _fm_session, _gt_session, _bsc_session
    proxy = cfg.get("proxy")
    _fm_session = _build_session(proxy, FM_HEADERS)
    _gt_session = _build_session(proxy, GT_HEADERS)
    _bsc_session = _build_session(proxy, DS_HEADERS)

    # 行情
    ticker = fm_ticker_prices()
    bnb = ticker.get("BNB", 0)
    if bnb <= 0:
        log.warning("BNB 价格获取失败, 使用默认 600")
        ticker["BNB"] = 600.0
    log.info("BNB=$%.2f", ticker.get("BNB", 0))

    now_ms = int(time.time() * 1000)

    # 加载队列
    queue_state = load_queue()
    existing_addrs = set(
        [t["address"] for t in queue_state.get("tokens", [])]
        + [t["address"] for t in queue_state.get("eliminated", [])]
    )

    # Step 1: 链上发现
    log.info("\n--- Step 1: 链上发现 ---")
    new_on_chain, latest_block = discover_on_chain(queue_state.get("lastBlock", 0))

    # Step 2: 入场筛 (返回 admitted + rejected)
    log.info("\n--- Step 2: 入场筛 ---")
    admitted, rejected_at_entry = admission_filter(new_on_chain, existing_addrs)

    # 将通过入场筛的代币加入队列
    if admitted:
        for item in admitted:
            token = item["token"]
            detail = item["detail"]
            queue_state["tokens"].append({
                "address": token["address"],
                "creator": token.get("creator", ""),
                "block": token.get("block", 0),
                "name": detail.get("name") or token.get("name", ""),
                "symbol": detail.get("shortName") or token.get("symbol", ""),
                "createdAt": token["createdAt"],
                "addedAt": now_ms,
                "totalSupply": detail["totalSupply"],
                "socialCount": detail["socialCount"],
                "socialLinks": detail["socialLinks"],
                "descr": detail.get("descr", ""),
                "price": detail["price"],
                "peakPrice": detail["price"],
                "holders": detail["holders"],
                "peakHolders": detail["holders"],
                "liquidity": detail.get("liquidity", 0),
                "peakLiquidity": detail.get("liquidity", 0),
                "raisedAmount": detail.get("raisedAmount", 0),
                "progress": detail.get("progress", 0),
                "day1Vol": detail.get("day1Vol", 0),
                "consecDrops": 0,
                "lastPrice": detail["price"],
                "priceHistory": [detail["price"]],
            })

    log.info("入队后: %d 个代币", len(queue_state["tokens"]))

    # Step 3: 淘汰检查 (v6: 返回 survivors, eliminated, 无 rpc_logs_map)
    log.info("\n--- Step 3: 淘汰检查 ---")
    survivors, eliminated = elimination_check(queue_state["tokens"], now_ms, bscscan_key)
    queue_state["tokens"] = survivors
    queue_state["eliminated"].extend([{
        "address": e["address"], "name": e.get("name", ""),
        "symbol": e.get("symbol", ""),
        "elimReason": e["elimReason"], "eliminatedAt": e["eliminatedAt"],
        "createdAt": e.get("createdAt", 0),
    } for e in eliminated])

    # 入场被拒的代币也记入 eliminated (避免重复入场筛)
    queue_state["eliminated"].extend([{
        "address": r["token"].get("address", ""),
        "name": (r.get("detail") or {}).get("name") or r["token"].get("name", ""),
        "symbol": (r.get("detail") or {}).get("shortName") or r["token"].get("symbol", ""),
        "elimReason": f"入场拒绝: {r.get('reason', '')}",
        "eliminatedAt": now_ms,
        "createdAt": r["token"].get("createdAt", 0),
    } for r in rejected_at_entry if r["token"].get("address")])

    log.info("淘汰后: %d 个存活, %d 个淘汰", len(survivors), len(eliminated))

    # Step 4: 仿盘检测 + 精筛
    log.info("\n--- Step 4: 仿盘检测 + 精筛 ---")

    # 仿盘检测 (本地队列统计, 零 API 调用)
    rejected_as_known = []
    for r in rejected_at_entry:
        token = r.get("token", {})
        detail = r.get("detail") or {}
        rejected_as_known.append({
            "address": token.get("address", ""),
            "name": detail.get("name") or token.get("name", ""),
            "symbol": detail.get("shortName") or token.get("symbol", ""),
        })
    all_known = queue_state.get("tokens", []) + queue_state.get("eliminated", []) + rejected_as_known

    # 更新持久化名称索引
    update_name_index(queue_state, all_known)

    all_to_check = survivors + eliminated
    copycat_map = detect_copycats(all_to_check, all_known,
                                  queue_state.get("nameIndex"))
    for t in survivors:
        cc = copycat_map.get(t.get("address", "").lower())
        if cc:
            t["copycat"] = cc
    for t in eliminated:
        cc = copycat_map.get(t.get("address", "").lower())
        if cc:
            t["copycat"] = cc

    # 精筛 (v6: 极简三条件, 无钱包分析)
    quality_results = quality_filter(survivors, now_ms)

    # 按持币数排序
    quality_results.sort(key=lambda x: (x.get("holders", 0)), reverse=True)

    log.info("精筛通过: %d/%d", len(quality_results), len(survivors))

    # 更新队列状态
    queue_state["lastBlock"] = latest_block
    queue_state["lastScanTime"] = now_ms
    save_queue(queue_state)

    # --- 构建输出 JSON ---
    from datetime import timedelta
    now_dt = datetime.now(timezone(timedelta(hours=8)))  # 北京时间
    scan_time_str = now_dt.strftime("%-Y-%-m-%-d %H:%M:%S")
    file_ts = now_dt.strftime("%Y-%m-%dT%H-%M-%S")

    # tokens: 精筛结果
    out_tokens = []
    for t in quality_results:
        cc = t.get("copycat", {})
        out_tokens.append({
            "address": t.get("address", ""),
            "name": t.get("name", ""),
            "symbol": t.get("symbol", ""),
            "holders": t.get("holders", 0),
            "created_at": t.get("createdAt", 0),
            "total_supply": t.get("totalSupply", 0),
            "price": t.get("price", 0),
            "max_price": t.get("peakPrice", 0),
            "age_hours": round((now_ms - t.get("createdAt", 0)) / 3600000, 2),
            "social_count": t.get("socialCount", 0),
            "social_links": t.get("socialLinks", {}),
            "copycat_count": cc.get("count", 0),
            "is_copycat": cc.get("isCopycat", False),
            "day1_vol": t.get("day1Vol", 0),
            "progress": t.get("progress", 0),
            "liquidity": t.get("liquidity", 0),
            "raised_amount": t.get("raisedAmount", 0),
        })

    # queue: 存活快照
    out_queue = []
    for t in survivors:
        cc = t.get("copycat", {})
        out_queue.append({
            "address": t.get("address", ""),
            "name": t.get("name", ""),
            "symbol": t.get("symbol", ""),
            "holders": t.get("holders", 0),
            "created_at": t.get("createdAt", 0),
            "price": t.get("price", 0),
            "peak_price": t.get("peakPrice", 0),
            "peak_holders": t.get("peakHolders", 0),
            "liquidity": t.get("liquidity", 0),
            "peak_liquidity": t.get("peakLiquidity", 0),
            "raised_amount": t.get("raisedAmount", 0),
            "progress": t.get("progress", 0),
            "social_count": t.get("socialCount", 0),
            "social_links": t.get("socialLinks", {}),
            "age_hours": round((now_ms - t.get("createdAt", 0)) / 3600000, 2),
            "consec_drops": t.get("consecDrops", 0),
            "copycat_count": cc.get("count", 0),
            "is_copycat": cc.get("isCopycat", False),
        })

    # eliminatedThisRound: 本轮淘汰
    out_eliminated = []
    for e in eliminated:
        cc = e.get("copycat", {})
        out_eliminated.append({
            "address": e.get("address", ""),
            "name": e.get("name", ""),
            "symbol": e.get("symbol", ""),
            "holders": e.get("holders", 0),
            "created_at": e.get("createdAt", 0),
            "price": e.get("price", 0),
            "peak_price": e.get("peakPrice", 0),
            "reason": e.get("elimReason", ""),
            "social_count": e.get("socialCount", 0),
            "age_hours": round((now_ms - e.get("createdAt", 0)) / 3600000, 2),
            "copycat_count": cc.get("count", 0),
            "is_copycat": cc.get("isCopycat", False),
        })

    # rejectedAtEntry: 入场被拒
    out_rejected = []
    for r in rejected_at_entry:
        token = r.get("token", {})
        detail = r.get("detail") or {}
        social_links = detail.get("socialLinks", {})
        out_rejected.append({
            "address": token.get("address", ""),
            "name": detail.get("name") or token.get("name", ""),
            "symbol": detail.get("shortName") or token.get("symbol", ""),
            "created_at": token.get("createdAt", 0),
            "reason": r.get("reason", ""),
            "holders": detail.get("holders", 0),
            "price": detail.get("price", 0),
            "social_count": detail.get("socialCount", 0),
            "social_links": social_links,
            "progress": detail.get("progress", 0),
            "age_hours": round((now_ms - token.get("createdAt", 0)) / 3600000, 2),
            "copycat_count": 0,
            "is_copycat": False,
        })

    output = {
        "scanTime": scan_time_str,
        "totalTokens": len(new_on_chain),
        "newDiscovered": len(new_on_chain),
        "newAdmitted": len(admitted),
        "eliminatedCount": len(eliminated),
        "filteredTokens": len(quality_results),
        "queueSize": len(survivors),
        "tokens": out_tokens,
        "queue": out_queue,
        "eliminatedThisRound": out_eliminated,
        "rejectedAtEntry": out_rejected,
    }

    # 写入 data/{timestamp}.json
    out_path = DATA_DIR / f"{file_ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info("输出: %s", out_path)

    # 清理过期数据文件
    cleanup_old_data(7)

    # 打印摘要
    log.info("=" * 50)
    log.info("扫描完成")
    log.info("  链上发现: %d", len(new_on_chain))
    log.info("  入场通过: %d, 入场淘汰: %d", len(admitted), len(rejected_at_entry))
    log.info("  队列淘汰: %d, 队列存活: %d", len(eliminated), len(survivors))
    log.info("  精筛通过: %d", len(quality_results))
    log.info("=" * 50)


if __name__ == "__main__":
    main()
