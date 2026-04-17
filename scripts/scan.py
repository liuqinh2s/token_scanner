"""
数据源: BSC RPC (链上事件) + four.meme API (详情) + DexScreener (价格) + GeckoTerminal (持币数)

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
  3. 淘汰检查 (~数秒): DexScreener 批量查价 + GeckoTerminal 持币数 + Detail API → 永久淘汰弃盘币
  4. 精筛 (瞬时): 动量筛选, 从存活币中找起飞信号
  5. 仿盘检测: 本地统计同名代币数量 (零 API 调用), 有大量仿盘(≥3)则标记

砍掉的慢环节 (v5 → v6):
  - GeckoTerminal K线 (每个代币 2s+)
  - BSCScan 钱包行为分析 (开发者/聪明钱)
  - 币安 Web3 聪明钱信号 + Token Dynamic
  - GMGN 聪明钱地址
  - BSCScan 持币数查询 (免费 key 不支持 BSC 链)
  - RPC Transfer 日志持币数 (bonding curve 阶段不产生标准 Transfer, 不准确)

淘汰条件 (永久剔除):
  - 价格从峰值跌 90%+
  - 持币地址从 ≥30 跌破 10
  - 持币数从峰值跌 70%+ (峰值≥50, 清理僵尸币)
  - 无社交媒体
  - 流动性从 >$1k 跌破 $100 (仅已毕业代币)
  - 进度 < 1% 且币龄 > 2h
  - 进度 < 5% 且币龄 > 4h
  - 进度从峰值跌 50%+ 且币龄 > 6h (热度消退)
  - 币龄 > 15min 且最高持币数 < 3
  - 币龄 > 1h 且最高持币数 < 5
  - 币龄 > 48h
  - 价格突破: 峰值价格 ≥ 0.0001 → 从队列移出到「已突破」列表, 冻结快照不再跟踪

精筛条件 (动量筛选):
  - 🚀 火箭通道 (快速起飞币, 跳过慢条件): 进度≥70% + 持币≥100 + 募资≥8BNB + 币龄≤30min + 价格≤5e-05
  - 🎓 毕业通道 (刚毕业强势币, 跳过慢条件): 已毕业 + 币龄≤2h + 持币≥100 + 流动性≥$10k + 价格≤1e-04 + 回撤≤30%
  - 币龄 ≥ 1 小时 (太短归零风险大)
  - 币龄 ≤ 36 小时 (太老失去市场关注)
  - 当前价 ≤ 0.00005 (离突破线太近利润空间不够)
  - 持币地址数 ≥ 15
  - 价格动量: 当前价 ≥ 入队价 × 1.5 或 当前价 ≥ 历史最低价 × 2.5
  - 持币增长: 当前持币 ≥ 入队持币 × 1.5 或 近 3 轮持续递增
  - 进度 ≥ 40% 且 < 97% (已毕业/接近毕业代币买入即亏)
  - 进度从 10%+ 跌破 5% 排除 (衰退信号)
  - 已毕业代币流动性 ≥ $500
  - 买卖比 ≥ 1.2 (买入笔数/卖出笔数, 买压>卖压)
  - 价格加速度 ≥ 15% (最近 2 轮价格上涨速率)
  - 进度加速度 ≥ 5% (最近 2 轮进度增长, 资金持续涌入信号)
  - 持币增速 ≥ 10% (最近 2 轮持币增长速率)
  - 回撤保护: 当前价 ≥ 峰值 × 0.5 (从峰值跌超 50% 不推)
  - 精筛冷却: 同一代币通过精筛后, 6 轮内不再重复推送
  - 仿盘数: 仅标记, 不排除 (仿盘多=热门信号, 交给用户判断)

精筛后防线 (深度检查, 仅对精筛通过的少量代币):
  - Top10 持仓占比 ≤ 85% (防庄家控盘)
  - 开发者行为: 清仓/撤池子 → 排除 (跑路信号)
  - 假K线检测: 15min K线无影线实体柱 ≥ 80% 或全阳线 ≥ 90% 或 1min K线脉冲死线(头部大涨后续全死线) → 排除 (控盘刷量, 会瞬间归零)
"""

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
from datetime import datetime, timezone, timedelta
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
QUALITY_MIN_AGE_MIN = 60               # 精筛: 币龄 ≥ 1 小时 (太短的币归零风险大, 让它先证明自己不是骗局)
QUALITY_MAX_AGE_HOURS = 36             # 精筛: 币龄 ≤ 36 小时 (太老的币失去市场关注, 动量不可信)
QUALITY_MAX_PRICE = 0.00005            # 精筛: 当前价 ≤ 0.00005 (离突破线太近利润空间不够, 不追)
QUALITY_MIN_HOLDERS = 15               # 精筛: 持币地址数 ≥ 15
QUALITY_PRICE_MOMENTUM_VS_ADDED = 1.5  # 精筛: 当前价 ≥ 入队价 × 1.5
QUALITY_PRICE_MOMENTUM_VS_LOW = 2.5    # 精筛: 当前价 ≥ 历史最低价 × 2.5
QUALITY_HOLDERS_GROWTH_VS_ADDED = 1.5  # 精筛: 当前持币 ≥ 入队持币 × 1.5
QUALITY_HOLDERS_CONSEC_ROUNDS = 3      # 精筛: 持币数连续递增轮数 ≥ 3
QUALITY_MIN_PROGRESS = 0.40            # 精筛: 进度 ≥ 40% (历史数据: 40%+代币avg 2.2x, 20-40%代币avg 1.4x)
QUALITY_MAX_PROGRESS = 0.97            # 精筛: 进度 < 97% (已毕业/接近毕业代币买入即亏, 排除)
QUALITY_PROGRESS_DROP_PEAK = 0.10     # 精筛: 进度曾达到 10% 以上
QUALITY_PROGRESS_DROP_FLOOR = 0.05    # 精筛: 进度从 10%+ 跌破 5% 则排除
QUALITY_MIN_LIQUIDITY_GRAD = 500      # 精筛: 已毕业代币流动性 ≥ $500
QUALITY_MAX_DRAWDOWN = 0.50           # 精筛: 当前价 ≥ 峰值 × 0.5 (回撤保护)
QUALITY_COOLDOWN_ROUNDS = 6           # 精筛: 同一代币冷却轮数 (通过后 N 轮内不再推送)
QUALITY_MIN_BUY_SELL_RATIO = 1.2      # 精筛: 买卖比 ≥ 1.2 (买入笔数/卖出笔数, 买压>卖压)
QUALITY_MIN_VOLUME_USD = 100          # 精筛: 24h 交易量 ≥ $100 (有真实交易活动)
QUALITY_MIN_PRICE_ACCEL = 0.15        # 精筛: 价格加速度 ≥ 15% (最近 2 轮价格变化率, 历史数据: ≥20%止盈率75%)
QUALITY_MIN_PROGRESS_ACCEL = 0.05     # 精筛: 进度加速度 ≥ 5% (最近 2 轮进度变化, 历史数据: ≥5%+价格涨≥10%止盈率91%)
QUALITY_MIN_HOLDERS_GROWTH_RATE = 0.10  # 精筛: 持币增速 ≥ 10% (最近 2 轮持币变化率)
QUALITY_MAX_TOP10_CONCENTRATION = 0.85  # 精筛: Top10 持仓占比 ≤ 85% (防庄家控盘)
QUALITY_FAKE_CANDLE_RATIO = 0.80       # 精筛: 实体柱占比 ≥ 80% 判定为假K线 (无影线=价格被控制)
QUALITY_FAKE_CANDLE_MIN_COUNT = 4      # 精筛: 至少 4 根 K线才做假K线检测 (数据不足不判断)
QUALITY_FAKE_CANDLE_WICK_THRESHOLD = 0.005  # 精筛: 影线/实体 < 0.5% 视为无影线 (允许极微小波动)
QUALITY_FAKE_CANDLE_BULLISH_RATIO = 0.90   # 精筛: 阳线占比 ≥ 90% 判定为假K线 (只涨不跌=控盘)
QUALITY_FAKE_CANDLE_DEAD_MIN_COUNT = 6     # 精筛: 死线检测最少 K线数 (1min K线, 至少 6 根)
QUALITY_FAKE_CANDLE_DEAD_RATIO = 0.70      # 精筛: 后续死线占比 ≥ 70% 判定为假阳线 (头部大涨后全是死线)
QUALITY_FAKE_CANDLE_DEAD_THRESHOLD = 0.005 # 精筛: 振幅 < 0.5% 视为死线 (几乎不动)
QUALITY_FAKE_CANDLE_SPIKE_MULTIPLE = 10    # 精筛: 头部K线振幅 ≥ 后续平均振幅 × 10 才算脉冲
COPYCAT_MARK_MIN = 3                   # 仿盘数 ≥3 标记 (仅标记, 不排除)
MIN_SOCIAL_COUNT = 1                   # 最少关联社交媒体数

# 火箭通道阈值 (快速起飞币专用, 跳过币龄/价格上限/动量等慢条件)
# 历史回测: 17 命中, 3 突破, 0 暴亏, 最差 x1.0 (不赚不亏)
ROCKET_MIN_PROGRESS = 0.70             # 火箭: 进度 ≥ 70% (资金涌入快, 接近毕业)
ROCKET_MIN_HOLDERS = 100               # 火箭: 持币 ≥ 100 (真实买盘, 非刷量)
ROCKET_MIN_RAISED = 8.0                # 火箭: 募资 ≥ 8 BNB (资金量充足)
ROCKET_MAX_AGE_HOURS = 0.5             # 火箭: 币龄 ≤ 30 分钟 (刚起飞, 不是老币)
ROCKET_MAX_PRICE = 0.00005             # 火箭: 买入价 ≤ 5e-05 (保留利润空间)

# 毕业通道阈值 (刚毕业且势头强劲的代币, 跳过进度/价格上限/动量等慢条件)
# 历史回测: 7 命中, 1 突破, 0 亏损, 2 个×1.5+
GRAD_MIN_HOLDERS = 100                 # 毕业: 持币 ≥ 100 (真实买盘)
GRAD_MAX_AGE_HOURS = 2.0              # 毕业: 币龄 ≤ 2 小时 (刚毕业不久)
GRAD_MIN_LIQUIDITY = 10000            # 毕业: 流动性 ≥ $10,000 (毕业后有足够深度)
GRAD_MAX_PRICE = 0.0001               # 毕业: 价格 ≤ 0.0001 (还没突破, 有利润空间)
GRAD_MIN_DRAWDOWN_RATIO = 0.70        # 毕业: 当前价 ≥ 峰值 × 0.7 (毕业后没大跌)

# 淘汰阈值
ELIM_PRICE_DROP_PCT = 0.90             # 价格从峰值跌 90%
ELIM_HOLDERS_FLOOR = 10               # 持币数跌破 10
ELIM_HOLDERS_PEAK_MIN = 30            # 持币数曾达到 30 才触发跌破淘汰
ELIM_HOLDERS_DROP_PCT = 0.70          # 持币数从峰值跌 70% 淘汰 (清理僵尸币, 数据显示 99 个僵尸币占位)
ELIM_HOLDERS_DROP_PEAK_MIN = 50       # 持币数曾达到 50 才触发跌幅淘汰 (避免误杀小币)
ELIM_LIQ_FLOOR = 100                  # 流动性跌破 $100
ELIM_LIQ_PEAK_MIN = 1000              # 流动性曾达到 $1000 才触发跌破淘汰
ELIM_PROGRESS_MIN = 0.01              # 进度 < 1%
ELIM_PROGRESS_AGE_HOURS = 2           # 进度<1%淘汰的币龄门槛
ELIM_PROGRESS_MIN_MID = 0.05          # 进度 < 5%
ELIM_PROGRESS_AGE_HOURS_MID = 4       # 进度<5%淘汰的币龄门槛
ELIM_PROGRESS_DROP_PCT = 0.50         # 进度从峰值跌 50% 淘汰 (热度消退)
ELIM_PROGRESS_DROP_AGE_HOURS = 6      # 进度跌幅淘汰的币龄门槛 (给新币缓冲时间)
ELIM_EARLY_PEAK_HOLDERS = 3           # 币龄>15min 最高持币数 < 3 淘汰
ELIM_EARLY_AGE_MIN = 0.25             # 15 分钟 = 0.25h
ELIM_MID_PEAK_HOLDERS = 5             # 币龄>1h 最高持币数 < 5 淘汰
ELIM_MID_AGE_HOURS = 1                # 1 小时

# 价格突破阈值 (突破后从队列移出, 冻结快照, 不再跟踪)
# 我们的目标价区间: 0.000001 ~ 0.0001, 突破 0.0001 的币不在买入范围内
BREAKTHROUGH_PRICE = 0.0001           # 峰值价格 ≥ 0.0001 视为已突破


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

        def _fetch_block_ts(bn: int) -> tuple[int, int]:
            res = _rpc_call("eth_getBlockByNumber", [hex(bn), False])
            if res and res.get("result"):
                ts = int(res["result"].get("timestamp", "0x0"), 16)
                if ts > 0:
                    return bn, ts
            return bn, 0

        with ThreadPoolExecutor(max_workers=10) as pool:
            for bn, ts in pool.map(_fetch_block_ts, block_nums):
                if ts > 0:
                    block_ts_map[bn] = ts

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
                # 提取买卖笔数 (DexScreener txns 字段)
                txns = p.get("txns") or {}
                # 汇总多个时间段: 优先用 h1 (最近 1h), 回退 h24
                txns_h1 = txns.get("h1") or {}
                txns_h24 = txns.get("h24") or {}
                buys_h1 = int(txns_h1.get("buys") or 0)
                sells_h1 = int(txns_h1.get("sells") or 0)
                buys_h24 = int(txns_h24.get("buys") or 0)
                sells_h24 = int(txns_h24.get("sells") or 0)
                result[addr] = {
                    "price": float(p.get("priceUsd") or 0),
                    "liquidity": float((p.get("liquidity") or {}).get("usd") or 0),
                    "volume24h": float((p.get("volume") or {}).get("h24") or 0),
                    "volumeH1": float((p.get("volume") or {}).get("h1") or 0),
                    "buysH1": buys_h1,
                    "sellsH1": sells_h1,
                    "buysH24": buys_h24,
                    "sellsH24": sells_h24,
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


def gt_holder_counts(addresses: list[str]) -> dict[str, int]:
    """
    GeckoTerminal token info API 查持币地址数 (免费, 无需 key)
    对已毕业代币 (bonding curve 结束后) 最准确
    对未毕业代币返回 None (GeckoTerminal 可能未收录)
    限流: ~30 req/min, 需控制请求速率
    """
    result = {}
    if not addresses:
        return result
    for addr in addresses:
        url = f"{GT_BASE}/networks/bsc/tokens/{addr}/info"
        data = _gt_request(url)
        if data:
            holders = (data.get("data", {}).get("attributes", {})
                       .get("holders", {}).get("count"))
            if holders and holders > 0:
                result[addr] = holders
        time.sleep(0.4)  # ~2.5 req/s, 留余量避免 429
    log.info("GT 查到 %d/%d 个代币持币数", len(result), len(addresses))
    return result


def bscscan_scrape_holder_count(token_address: str) -> int | None:
    """
    爬取 BSCScan 网页获取持币地址数 (降级备选)
    BSCScan 网页端有持币数显示, 但 API 免费 key 不支持
    """
    _ensure_sessions()
    try:
        url = f"https://bscscan.com/token/{token_address}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html",
        }
        r = _bsc_session.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        # BSCScan 页面 meta 标签: "Holders: 369 | As at ..."
        m = re.search(r'Holders:\s*([\d,]+)', r.text)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception as e:
        log.debug("BSCScan 网页爬取失败 [%s]: %s", token_address[:16], e)
    return None


def graduated_holder_counts(addresses: list[str]) -> dict[str, int]:
    """
    BSCScan 网页爬取持币数 (并发, ~2s/个)
    用于: 已毕业代币 + detail API 返回 holders==0 的代币
    BSCScan 网页端有持币数, 免费 API 不支持, 直接爬网页
    GeckoTerminal 作为备选 (太慢, 仅在 BSCScan 失败时使用)
    """
    result = {}
    if not addresses:
        return result

    # 并发爬取 BSCScan 网页 (3 线程, 避免被限流)
    def _scrape_one(addr: str) -> tuple[str, int | None]:
        count = bscscan_scrape_holder_count(addr)
        return addr, count

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_scrape_one, addr) for addr in addresses]
        for f in as_completed(futures):
            addr, count = f.result()
            if count is not None and count > 0:
                result[addr] = count

    log.info("BSCScan 网页查到 %d/%d 个代币持币数", len(result), len(addresses))

    # BSCScan 没查到的, 用 GT 补 (逐个查, 较慢, 仅补漏)
    missing = [a for a in addresses if a not in result]
    if missing and len(missing) <= 5:
        log.info("BSCScan 未覆盖 %d 个, 尝试 GT 补全...", len(missing))
        gt_result = gt_holder_counts(missing)
        result.update(gt_result)
        if gt_result:
            log.info("GT 补全 %d 个", len(gt_result))
    elif missing:
        log.info("BSCScan 未覆盖 %d 个, 跳过 GT (数量过多)", len(missing))

    return result


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


def gt_ohlcv_1min(token_address: str, limit: int = 30) -> list[list]:
    """拿 1 分钟 K线 (用于假阳线死线检测, 需要细粒度数据)"""
    url = f"{GT_BASE}/networks/bsc/pools/{token_address}/ohlcv/minute?aggregate=1&limit={limit}"
    data = _gt_request(url)
    if not data:
        return []
    return (data.get("data", {}).get("attributes", {}).get("ohlcv_list", []))


def gt_batch_peak_prices(tokens: list[dict]) -> dict[str, float]:
    """
    批量查询 GeckoTerminal 15 分钟 K线, 提取每个代币的最高价
    用于补充 peakPrice (扫描间隔内的价格冲高不会被 DexScreener 实时价捕获)
    OHLCV 格式: [timestamp, open, high, low, close, volume], c[2] = high

    K线数量策略:
      - 首次修复 (无 peakFixed 标记): 拉 48 根 (12h), 覆盖老币历史冲高
      - 后续轮次 (已有 peakFixed 标记): 拉 4 根 (1h), 只补上一轮间隔的遗漏
    限流策略: 串行查询, 每个请求间隔 2s, 避免触发 GT 30 req/min 限制
    """
    result = {}
    if not tokens:
        return result
    for t in tokens:
        addr = t["address"]
        # 首次修复拉 48 根 (12h 历史), 后续只拉 4 根 (1h)
        limit = 4 if t.get("peakFixed") else 48
        try:
            candles = gt_ohlcv_15min(addr, limit=limit)
            if candles:
                high = max(float(c[2]) for c in candles)
                if high > 0:
                    result[addr] = high
        except Exception as e:
            log.debug("GT K线峰值查询失败 [%s]: %s", addr[:16], e)
        time.sleep(2)  # 限流: ~30 req/min
    return result


def detect_fake_candles(candles: list[list], candles_1m: list[list] | None = None) -> dict:
    """
    假K线检测: 识别价格被人为控制的代币
    特征 1: K线几乎全是实体柱, 没有上下影线 — 价格被精确控制
    特征 2: 几乎没有阴线 (收盘<开盘), 全是阳线 — 真实市场不可能只涨不跌
    特征 3: 只有头一两根大涨K线, 后面全是极小振幅的死线 — 脉冲拉盘后无人接盘
    这类币看似只涨不跌, 实际随时会一分钟内归零

    OHLCV 格式: [timestamp, open, high, low, close, volume]
    判定逻辑 (满足任一即判定为假K线):
      A. 无影线实体柱占比 ≥ 80% → 价格被精确控制
      B. 阳线占比 ≥ 90% (几乎没有阴线) → 只涨不跌不符合真实市场
      C. 头部脉冲 + 后续死线占比 ≥ 70% (1min K线) → 一根阳线拉盘后全是死线

    参数:
      candles: 15min K线 (用于特征 A/B)
      candles_1m: 1min K线 (用于特征 C, 可选)

    返回: {fake: bool, reason: str, ratio: float, total: int, no_wick: int,
           bullish_ratio: float, bullish: int,
           dead_line_ratio: float, dead_lines: int, spike_ratio: float}
    """
    base_result = {"fake": False, "reason": "", "ratio": 0, "total": 0,
                   "no_wick": 0, "bullish_ratio": 0, "bullish": 0,
                   "dead_line_ratio": 0, "dead_lines": 0, "spike_ratio": 0}

    if not candles or len(candles) < QUALITY_FAKE_CANDLE_MIN_COUNT:
        return base_result

    no_wick_count = 0
    bullish_count = 0  # 阳线数 (收盘 >= 开盘)
    valid_count = 0

    for c in candles:
        o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
        # 跳过无效K线 (价格为 0 或 high == low 即无交易)
        if h <= 0 or l <= 0 or h == l:
            continue
        valid_count += 1

        # 统计阳线 (收盘 >= 开盘)
        if cl >= o:
            bullish_count += 1

        body = abs(cl - o)
        # 实体为 0 (十字星) 不算无影线
        if body <= 0:
            continue
        upper_wick = h - max(o, cl)
        lower_wick = min(o, cl) - l
        total_wick = upper_wick + lower_wick
        wick_ratio = total_wick / body if body > 0 else 0
        if wick_ratio < QUALITY_FAKE_CANDLE_WICK_THRESHOLD:
            no_wick_count += 1

    if valid_count < QUALITY_FAKE_CANDLE_MIN_COUNT:
        return {**base_result, "total": valid_count, "no_wick": no_wick_count,
                "bullish": bullish_count}

    wick_ratio = no_wick_count / valid_count
    bullish_ratio = bullish_count / valid_count

    # 判定: 满足任一条件即为假K线
    fake = False
    reason = ""
    if wick_ratio >= QUALITY_FAKE_CANDLE_RATIO:
        fake = True
        reason = "无影线实体柱 {}/{} ({:.0f}%)".format(no_wick_count, valid_count, wick_ratio * 100)
    if bullish_ratio >= QUALITY_FAKE_CANDLE_BULLISH_RATIO:
        if fake:
            reason += " + 全阳线 {}/{} ({:.0f}%)".format(bullish_count, valid_count, bullish_ratio * 100)
        else:
            fake = True
            reason = "全阳线 {}/{} ({:.0f}%)".format(bullish_count, valid_count, bullish_ratio * 100)

    # 特征 C: 头部脉冲 + 后续死线检测 (使用 1min K线)
    dead_line_ratio = 0
    dead_lines = 0
    spike_ratio_val = 0
    if candles_1m and len(candles_1m) >= QUALITY_FAKE_CANDLE_DEAD_MIN_COUNT:
        # 按时间排序 (升序, 最早的在前)
        sorted_1m = sorted(candles_1m, key=lambda c: c[0])
        # 计算每根K线的振幅 (high - low) / low
        amplitudes = []
        for c in sorted_1m:
            h_1m, l_1m = float(c[2]), float(c[3])
            if l_1m > 0 and h_1m > 0:
                amp = (h_1m - l_1m) / l_1m
                amplitudes.append(amp)
            else:
                amplitudes.append(0)

        if len(amplitudes) >= QUALITY_FAKE_CANDLE_DEAD_MIN_COUNT:
            # 找头部最大振幅K线 (前 3 根中振幅最大的)
            head_count = min(3, len(amplitudes))
            head_max_amp = max(amplitudes[:head_count])
            head_max_idx = amplitudes[:head_count].index(head_max_amp)

            # 后续K线 = 头部脉冲之后的所有K线
            tail_start = head_max_idx + 1
            tail_amps = amplitudes[tail_start:]

            if len(tail_amps) >= 3 and head_max_amp > 0:
                # 统计死线数量 (振幅 < 0.5%)
                dead_count = sum(1 for a in tail_amps if a < QUALITY_FAKE_CANDLE_DEAD_THRESHOLD)
                dead_line_ratio = dead_count / len(tail_amps)
                dead_lines = dead_count

                # 计算头部脉冲与后续平均振幅的倍数
                tail_avg = sum(tail_amps) / len(tail_amps) if tail_amps else 0
                spike_ratio_val = head_max_amp / tail_avg if tail_avg > 0 else float('inf')

                # 判定: 头部脉冲振幅 ≥ 后续平均 × 10 且 后续死线占比 ≥ 70%
                if (spike_ratio_val >= QUALITY_FAKE_CANDLE_SPIKE_MULTIPLE
                        and dead_line_ratio >= QUALITY_FAKE_CANDLE_DEAD_RATIO):
                    dead_reason = "脉冲死线 头部振幅{:.1f}%是后续{:.1f}倍 + 死线{}/{} ({:.0f}%)".format(
                        head_max_amp * 100, spike_ratio_val,
                        dead_count, len(tail_amps), dead_line_ratio * 100)
                    if fake:
                        reason += " + " + dead_reason
                    else:
                        fake = True
                        reason = dead_reason

    return {
        "fake": fake,
        "reason": reason,
        "ratio": round(wick_ratio, 3),
        "total": valid_count,
        "no_wick": no_wick_count,
        "bullish_ratio": round(bullish_ratio, 3),
        "bullish": bullish_count,
        "dead_line_ratio": round(dead_line_ratio, 3),
        "dead_lines": dead_lines,
        "spike_ratio": round(spike_ratio_val, 1) if spike_ratio_val != float('inf') else 999,
    }


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

    now_ms = int(time.time() * 1000)
    max_age_ms = MAX_AGE_HOURS * 3600 * 1000

    # 并发查询 detail (5 线程)
    detail_map = {}
    def _query_detail(addr: str) -> tuple[str, dict | None]:
        d = fm_detail(addr)
        if d is None:
            time.sleep(0.5)
            d = fm_detail(addr)  # 重试 1 次
        return addr, d

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_query_detail, t["address"]) for t in fresh]
        for f in as_completed(futures):
            addr, detail = f.result()
            if detail:
                detail_map[addr] = detail

    # 逐个判断入场条件
    for t in fresh:
        detail = detail_map.get(t["address"])
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

    log.info("入场筛: 通过 %d/%d (淘汰 %d: 无社交/总量不符)",
             len(admitted), len(fresh), len(rejected))
    return admitted, rejected


# ===================================================================
#  Step 3: 淘汰检查 — DexScreener + four.meme detail + BSCScan
# ===================================================================
def elimination_check(queue: list[dict], now_ms: int,
                      api_key: str) -> tuple[list[dict], list[dict], list[dict]]:
    """
    淘汰检查: 对队列中代币定期检查, 永久淘汰弃盘币
    持币数: BSCScan 网页爬取 (已毕业 + detail返回0的) > detail API (未毕业) > 缓存
    返回: (survivors, eliminated, breakthrough)
      - breakthrough: 峰值价格突破 0.0001 的代币, 冻结快照不再跟踪
    """
    survivors = []
    eliminated = []
    breakthrough = []

    if not queue:
        return survivors, eliminated, breakthrough

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
        return survivors, eliminated, breakthrough

    # 1b. 本地预淘汰: 用已有数据快速剔除明显垃圾币 (零 API 调用)
    # 注意: peakHolders==0 但进度>0 说明有真实买盘但 API 持币数据不准, 跳过预淘汰
    pre_filtered = []
    for t in age_filtered:
        age_hours = (now_ms - t.get("createdAt", 0)) / 3600000
        elim_reason = None
        peak_h = t.get("peakHolders", 0)

        # 进度>0 说明有资金进入, 但 holders==0 是 API 数据不准, 跳过让后续 API 更新
        if peak_h == 0 and t.get("peakProgress", 0) > 0:
            pass
        elif age_hours > ELIM_EARLY_AGE_MIN and peak_h < ELIM_EARLY_PEAK_HOLDERS:
            elim_reason = f"币龄{age_hours:.1f}h 最高持币仅{peak_h}"
        elif age_hours > ELIM_MID_AGE_HOURS and peak_h < ELIM_MID_PEAK_HOLDERS:
            elim_reason = f"币龄{age_hours:.1f}h 最高持币仅{peak_h}"

        if elim_reason:
            eliminated.append({**t, "eliminatedAt": now_ms, "elimReason": elim_reason})
        else:
            pre_filtered.append(t)

    pre_elim_count = len(age_filtered) - len(pre_filtered)
    if pre_elim_count > 0:
        log.info("淘汰: 本地预淘汰 %d 个 (持币数不足)", pre_elim_count)

    if not pre_filtered:
        return survivors, eliminated, breakthrough

    # 2. DexScreener + four.meme detail + 已毕业代币持币数 并行查询
    addrs = [t["address"] for t in pre_filtered]

    # 新入队代币 (本轮刚加入) 不需要再查 detail, 入场筛已经查过
    need_detail = [t for t in pre_filtered if now_ms - t.get("addedAt", 0) > 60000]

    # 需要 BSCScan 爬取持币数的代币:
    # 1. 已毕业代币 (progress >= 1): detail API 毕业后返回 0, 必须用链上数据
    # 2. 缓存 holders==0 的代币: detail API 数据不准, 用 BSCScan 兜底拿真实持币数
    bscscan_addrs = list({t["address"] for t in pre_filtered
                          if t.get("progress", 0) >= 1
                          or t.get("holders", 0) == 0})

    log.info("淘汰检查: 并行查询 %d 个代币 (DS + detail(%d个) + BSCScan持币数(%d个))...",
             len(pre_filtered), len(need_detail), len(bscscan_addrs))

    def _fetch_all_details():
        """并发查询 fm_detail, 5 线程, 失败重试 1 次"""
        detail_map = {}
        def _query_one(addr: str) -> tuple[str, dict | None]:
            detail = fm_detail(addr)
            if detail is None:
                time.sleep(0.5)
                detail = fm_detail(addr)  # 重试 1 次
            return addr, detail
        with ThreadPoolExecutor(max_workers=5) as inner_pool:
            futures = [inner_pool.submit(_query_one, t["address"]) for t in need_detail]
            for f in as_completed(futures):
                addr, detail = f.result()
                if detail:
                    detail_map[addr] = detail
        return detail_map

    with ThreadPoolExecutor(max_workers=3) as pool:
        ds_future = pool.submit(ds_batch_prices, addrs)
        detail_future = pool.submit(_fetch_all_details)
        grad_future = pool.submit(graduated_holder_counts, bscscan_addrs)
        ds_data = ds_future.result()
        detail_map = detail_future.result()
        grad_holders = grad_future.result()

    # 3. 逐个检查淘汰条件
    for t in pre_filtered:
        ds = ds_data.get(t["address"], {})
        detail = detail_map.get(t["address"])
        age_hours = (now_ms - t.get("createdAt", 0)) / 3600000

        # 更新动态数据 (DexScreener > detail > 队列缓存)
        current_price = (ds.get("price")
                         or (detail["price"] if detail else 0)
                         or t.get("price", 0))
        # 持币数优先级:
        # 已毕业: BSCScan (链上索引) > 缓存 (detail 毕业后返回 0, 不用)
        # 未毕业: detail API > BSCScan 兜底 (detail 返回 0 时) > 缓存
        # 注意: 用 None 区分"没查到"和"查到了但值为 0", 避免错误回退缓存
        _grad_h = grad_holders.get(t["address"])  # None = 没查到
        _det_h = detail["holders"] if detail else None  # None = detail 没查到
        _cache_h = t.get("holders", 0)
        is_graduated = t.get("progress", 0) >= 1
        if is_graduated:
            current_holders = _grad_h if _grad_h is not None else _cache_h
        else:
            # 未毕业: detail 有值且 >0 用 detail, 否则用 BSCScan 兜底, 最后回退缓存
            if _det_h is not None and _det_h > 0:
                current_holders = _det_h
            elif _grad_h is not None:
                current_holders = _grad_h
            else:
                current_holders = _cache_h
        # 调试: 记录持币数来源 (仅对持币数有变化的代币)
        if current_holders != _cache_h:
            if is_graduated:
                _src = "BSCScan" if _grad_h is not None else "缓存"
            elif _det_h is not None and _det_h > 0:
                _src = "detail"
            elif _grad_h is not None:
                _src = "BSCScan兜底"
            else:
                _src = "缓存"
            log.info("  持币数更新 %s: %d→%d (来源:%s)",
                     t.get("name", t["address"][:16]),
                     _cache_h, current_holders, _src)
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
            # 更新交易量和买卖笔数 (DexScreener)
            t["volume24h"] = ds.get("volume24h", 0)
            t["volumeH1"] = ds.get("volumeH1", 0)
            t["buysH1"] = ds.get("buysH1", 0)
            t["sellsH1"] = ds.get("sellsH1", 0)
            t["buysH24"] = ds.get("buysH24", 0)
            t["sellsH24"] = ds.get("sellsH24", 0)

        # 更新峰值 (仅用 DexScreener 实时价)
        t["peakPrice"] = max(t.get("peakPrice", 0), current_price)
        t["peakHolders"] = max(t.get("peakHolders", 0), current_holders)
        t["peakLiquidity"] = max(t.get("peakLiquidity", 0), current_liq)
        t["peakProgress"] = max(t.get("peakProgress", 0), current_progress)

        # 记录价格历史 (只保留最近 5 轮, 与 holdersHistory 对齐)
        price_hist = t.get("priceHistory", [])
        price_hist.append(current_price)
        t["priceHistory"] = price_hist[-5:]

        # 记录进度历史 (只保留最近 5 轮, 与 priceHistory 对齐)
        prog_hist = t.get("progressHistory", [])
        prog_hist.append(current_progress)
        t["progressHistory"] = prog_hist[-5:]

        # 连续下跌计数
        last_price = t.get("lastPrice", 0)
        if last_price > 0 and current_price < last_price:
            t["consecDrops"] = t.get("consecDrops", 0) + 1
        else:
            t["consecDrops"] = 0
        t["lastPrice"] = current_price

        # --- 价格突破检测: 峰值 ≥ 0.0001 → 移出队列, 冻结快照 ---
        if t.get("peakPrice", 0) >= BREAKTHROUGH_PRICE:
            t["breakthroughAt"] = now_ms
            breakthrough.append(t)
            log.info("  🚀 价格突破 %s — 峰值 %.2e (移出队列)",
                     t.get("name") or t["address"][:16], t["peakPrice"])
            continue

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

        # 4. 流动性从 >$1k 跌破 $100 (仅已毕业代币, 未毕业流动性数据不准确)
        if not elim_reason and is_graduated:
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

        # 9. 持币数从峰值跌 70% (清理僵尸币, 避免占位)
        if not elim_reason:
            peak_h = t.get("peakHolders", 0)
            if (peak_h >= ELIM_HOLDERS_DROP_PEAK_MIN
                    and current_holders < peak_h * (1 - ELIM_HOLDERS_DROP_PCT)):
                elim_reason = (f"持币数跌{(1 - current_holders / peak_h) * 100:.0f}% "
                               f"({peak_h}→{current_holders})")

        # 10. 进度从峰值跌 50% 且币龄 > 6h (热度消退)
        if not elim_reason:
            peak_prog = t.get("peakProgress", 0)
            if (age_hours > ELIM_PROGRESS_DROP_AGE_HOURS
                    and peak_prog > 0.10
                    and current_progress < peak_prog * (1 - ELIM_PROGRESS_DROP_PCT)):
                elim_reason = (f"进度跌{(1 - current_progress / peak_prog) * 100:.0f}% "
                               f"({peak_prog * 100:.1f}%→{current_progress * 100:.1f}%)")

        if elim_reason:
            eliminated.append({**t, "eliminatedAt": now_ms, "elimReason": elim_reason})
        else:
            survivors.append(t)

    elim_count = len(eliminated) - (len(queue) - len(pre_filtered))
    if elim_count > 0:
        log.info("淘汰: 条件淘汰 %d 个", elim_count)
        for e in eliminated[-elim_count:]:
            log.info("  ✗ %s — %s", e.get("name") or e["address"][:16], e["elimReason"])
    if breakthrough:
        log.info("价格突破: %d 个代币移出队列", len(breakthrough))

    return survivors, eliminated, breakthrough


# ===================================================================
#  Step 5: 精筛 — 动量筛选
# ===================================================================
def quality_filter(candidates: list[dict], now_ms: int,
                   cooldown_map: dict[str, int] | None = None,
                   current_round: int = 0) -> list[dict]:
    """
    精筛: 动量筛选 — 从队列存活币中找"正在起飞"的信号
    三通道:
      🚀 火箭通道 (快速起飞币, 跳过币龄/动量等慢条件):
        - 进度 ≥ 70% (资金涌入快, 接近毕业)
        - 持币 ≥ 100 (真实买盘, 非刷量)
        - 募资 ≥ 8 BNB (资金量充足)
        - 币龄 ≤ 30 分钟 (刚起飞, 不是老币)
        - 价格 ≤ 5e-05 (保留利润空间)
      🎓 毕业通道 (刚毕业且势头强劲, 跳过进度/价格上限/动量等慢条件):
        - 已毕业 (进度 = 100%)
        - 币龄 ≤ 2 小时 (刚毕业不久)
        - 持币 ≥ 100 (真实买盘)
        - 流动性 ≥ $10,000 (毕业后有足够深度)
        - 价格 ≤ 0.0001 (还没突破, 有利润空间)
        - 回撤保护: 当前价 ≥ 峰值 × 0.7 (毕业后没大跌)
      常规通道 (全部满足):
      - 币龄 ≥ 15 分钟 (数据稳定后再判断, 历史数据显示 0-1h 币龄代币亏损率高)
      - 持币地址数 ≥ 15
      - 价格动量: 当前价 ≥ 入队价 × 1.5 或 当前价 ≥ 历史最低价 × 2.5
      - 持币增长: 当前持币 ≥ 入队持币 × 1.5 或 近 3 轮持续递增
      - 进度 ≥ 40% 且 < 97% (进度低表现差; 已毕业/接近毕业代币买入即亏)
      - 进度从 10%+ 跌破 5% 排除 (衰退信号)
      - 已毕业代币流动性 ≥ $500
      - 买卖比 ≥ 1.2 (买压 > 卖压, 有人在吸筹)
      - 价格加速度 ≥ 15% (最近 2 轮价格上涨速率)
      - 进度加速度 ≥ 5% (最近 2 轮进度增长, 资金持续涌入)
      - 持币增速 ≥ 10% (最近 2 轮持币增长速率)
      - 回撤保护: 当前价 ≥ 峰值 × 0.5 (从峰值跌超 50% 不推)
      - 精筛冷却: 同一代币通过精筛后, 6 轮内不再重复推送
      - 仿盘数: 仅标记, 不排除 (仿盘多=热门信号, 交给用户判断)
      - [精筛后防线] Top10 持仓占比 ≤ 85% (防庄家控盘)
      - [精筛后防线] 开发者行为检查 (清仓/撤池子 → 排除)
      - [精筛后防线] 假K线检测 (无影线实体柱 ≥ 80% → 排除, 控盘刷量)

    参数:
      cooldown_map: {address: last_pass_round} 记录每个代币上次通过精筛的轮次
      current_round: 当前扫描轮次编号
    """
    results = []
    min_age_ms = QUALITY_MIN_AGE_MIN * 60 * 1000
    if cooldown_map is None:
        cooldown_map = {}

    for t in candidates:
        age_ms = now_ms - t.get("createdAt", 0)
        current_price = t.get("price", 0)
        holders = t.get("holders", 0)
        name = t.get("name") or t.get("address", "")[:16]
        addr = t.get("address", "")

        # 冷却期检查: 同一代币通过精筛后 N 轮内不再推送
        last_pass = cooldown_map.get(addr, -999)
        if current_round - last_pass < QUALITY_COOLDOWN_ROUNDS:
            continue

        age_hours = age_ms / 3600000
        progress = t.get("progress", 0)
        raised = t.get("raisedAmount", 0)

        # ============================================================
        # 火箭通道: 高进度 + 高持币 + 高募资 + 低币龄 → 跳过慢条件直接通过
        # 捕捉快速起飞币 (如老登: 21min 内从 2e-05 涨到 9.46e-05)
        # 历史回测: 17 命中, 3 突破, 0 暴亏
        # ============================================================
        is_rocket = (progress >= ROCKET_MIN_PROGRESS
                     and holders >= ROCKET_MIN_HOLDERS
                     and raised >= ROCKET_MIN_RAISED
                     and age_hours <= ROCKET_MAX_AGE_HOURS
                     and 0 < current_price <= ROCKET_MAX_PRICE)

        if is_rocket:
            t["isRocket"] = True
            results.append(t)
            cooldown_map[addr] = current_round
            log.info("精筛: 🚀 火箭通道 %s — 价格 %.3e, 持币 %d, 进度 %.1f%%, 募资 %.1fBNB, 币龄 %.0fmin",
                     name, current_price, holders, progress * 100, raised, age_ms / 60000)
            continue

        # ============================================================
        # 毕业通道: 刚毕业 + 高持币 + 高流动性 + 价格未突破 → 跳过慢条件
        # 捕捉快速毕业后继续涨的代币 (如 FREEDOM: 15min 毕业, 毕业后 ×1.5)
        # 历史回测: 7 命中, 1 突破, 0 亏损, 2 个×1.5+
        # ============================================================
        liq = t.get("liquidity", 0)
        peak_price = t.get("peakPrice", 0)
        is_graduated = (progress >= 1.0
                        and age_hours <= GRAD_MAX_AGE_HOURS
                        and holders >= GRAD_MIN_HOLDERS
                        and liq >= GRAD_MIN_LIQUIDITY
                        and 0 < current_price <= GRAD_MAX_PRICE
                        and (peak_price <= 0 or current_price >= peak_price * GRAD_MIN_DRAWDOWN_RATIO))

        if is_graduated:
            t["isGraduated"] = True
            results.append(t)
            cooldown_map[addr] = current_round
            log.info("精筛: 🎓 毕业通道 %s — 价格 %.3e, 持币 %d, 流动性 $%.0f, 币龄 %.0fmin",
                     name, current_price, holders, liq, age_ms / 60000)
            continue

        # ============================================================
        # 常规精筛: 动量筛选 (需要多轮数据积累)
        # ============================================================

        # 条件 1: 币龄 ≥ 1 小时 (太短的币归零风险大)
        if age_ms < min_age_ms:
            continue

        # 条件 1b: 币龄 ≤ 36 小时 (太老的币失去市场关注, 动量不可信)
        if age_hours > QUALITY_MAX_AGE_HOURS:
            continue

        # 条件 2: 持币地址数 ≥ 15
        if holders < QUALITY_MIN_HOLDERS:
            continue

        # 条件 2b: 当前价 ≤ 0.00005 (离突破线太近利润空间不够, 不追)
        if current_price > QUALITY_MAX_PRICE:
            continue

        # 条件 3: 价格动量 (二选一)
        if current_price <= 0:
            continue
        added_price = t.get("addedPrice", 0)
        price_hist = t.get("priceHistory", [])
        min_price = min(price_hist) if price_hist else 0
        price_ok = False
        if added_price > 0 and current_price >= added_price * QUALITY_PRICE_MOMENTUM_VS_ADDED:
            price_ok = True
        if min_price > 0 and current_price >= min_price * QUALITY_PRICE_MOMENTUM_VS_LOW:
            price_ok = True
        if not price_ok:
            continue

        # 条件 3b: 回撤保护 — 当前价不能低于峰值的 50%
        peak_price = t.get("peakPrice", 0)
        if peak_price > 0 and current_price < peak_price * QUALITY_MAX_DRAWDOWN:
            continue

        # 条件 4: 持币增长 (二选一)
        added_holders = t.get("addedHolders", 0)
        h_hist = t.get("holdersHistory", [])
        holders_ok = False
        if added_holders > 0 and holders >= added_holders * QUALITY_HOLDERS_GROWTH_VS_ADDED:
            holders_ok = True
        if len(h_hist) >= QUALITY_HOLDERS_CONSEC_ROUNDS:
            consec = True
            for i in range(-QUALITY_HOLDERS_CONSEC_ROUNDS + 1, 0):
                if h_hist[i] <= h_hist[i - 1]:
                    consec = False
                    break
            if consec:
                holders_ok = True
        if not holders_ok:
            continue

        # 条件 5: 进度 ≥ 40% 且 < 97% (已毕业/接近毕业代币买入即亏)
        if progress < QUALITY_MIN_PROGRESS:
            continue
        if progress >= QUALITY_MAX_PROGRESS:
            continue

        # 条件 5b: 进度从 10%+ 跌破 5% 排除 (曾经有热度但在衰退)
        peak_progress = t.get("peakProgress", 0)
        if peak_progress >= QUALITY_PROGRESS_DROP_PEAK and progress < QUALITY_PROGRESS_DROP_FLOOR:
            continue

        # 条件 5c: 已毕业代币流动性 ≥ $500 (流动性枯竭的僵尸币排除)
        liq = t.get("liquidity", 0)
        if progress >= 1 and liq > 0 and liq < QUALITY_MIN_LIQUIDITY_GRAD:
            continue

        # 条件 6: 买卖比 ≥ 1.2 (买压 > 卖压, 有人在吸筹)
        # 优先用 h1 数据 (更灵敏), 回退 h24
        buys = t.get("buysH1", 0) or t.get("buysH24", 0)
        sells = t.get("sellsH1", 0) or t.get("sellsH24", 0)
        if sells > 0:
            buy_sell_ratio = buys / sells
            if buy_sell_ratio < QUALITY_MIN_BUY_SELL_RATIO:
                continue
        # buys > 0 且 sells == 0 → 全是买入, 通过
        # buys == 0 且 sells == 0 → bonding curve 阶段无 DEX 数据, 跳过此条件

        # 条件 7: 价格加速度 (最近 2 轮价格变化率 ≥ 5%)
        if len(price_hist) >= 2:
            prev_price = price_hist[-2]
            if prev_price > 0:
                price_accel = (current_price - prev_price) / prev_price
                if price_accel < QUALITY_MIN_PRICE_ACCEL:
                    continue

        # 条件 8: 持币增速 (最近 2 轮持币变化率 ≥ 10%)
        if len(h_hist) >= 2:
            prev_holders = h_hist[-2]
            if prev_holders > 0:
                holders_growth_rate = (holders - prev_holders) / prev_holders
                if holders_growth_rate < QUALITY_MIN_HOLDERS_GROWTH_RATE:
                    continue

        # 条件 9: 进度加速度 (最近 2 轮进度变化 ≥ 5%)
        prog_hist = t.get("progressHistory", [])
        if len(prog_hist) >= 2:
            prev_prog = prog_hist[-2]
            prog_accel = progress - prev_prog
            if prog_accel < QUALITY_MIN_PROGRESS_ACCEL:
                continue

        # 条件 10: 仿盘标记 (不排除, 仅标记)

        results.append(t)
        # 记录本轮通过精筛
        cooldown_map[addr] = current_round
        added_price = t.get("addedPrice", 0)
        added_holders = t.get("addedHolders", 0)
        price_mult = current_price / added_price if added_price > 0 else 0
        holders_mult = holders / added_holders if added_holders > 0 else 0
        buys = t.get("buysH1", 0) or t.get("buysH24", 0)
        sells = t.get("sellsH1", 0) or t.get("sellsH24", 0)
        _bs_ratio = buys / sells if sells > 0 else float('inf') if buys > 0 else 0
        log.info("精筛: ✓ %s — 价格 %.3e (×%.1f), 持币 %d (×%.1f), 进度 %.1f%%, 买卖比 %.1f, 币龄 %.1fmin",
                 name, current_price, price_mult, holders, holders_mult,
                 progress * 100, _bs_ratio, age_ms / 60000)

    return results


def post_quality_defense(candidates: list[dict], api_key: str) -> list[dict]:
    """
    精筛后防线: 对精筛通过的少量代币做深度检查 (仅个位数, 不影响速度)
    1. Top10 持仓集中度: BSCScan Top Holders → 占比 > 85% 排除 (庄家控盘)
    2. 开发者行为: BSCScan Transfer → 清仓/撤池子 排除 (跑路信号)
    3. 假K线检测: GeckoTerminal 15min+1min K线 → 无影线≥80%/全阳线≥90%/脉冲死线 排除 (控盘刷量)
    """
    if not candidates or not api_key:
        return candidates

    log.info("精筛后防线: 检查 %d 个代币 (Top Holder + 开发者行为)...", len(candidates))

    results = []
    for t in candidates:
        addr = t.get("address", "")
        name = t.get("name") or addr[:16]
        creator = t.get("creator", "")
        total_supply = t.get("totalSupply", TOTAL_SUPPLY)
        exclude_reason = None

        # 1. Top10 持仓集中度检查
        try:
            top_holders = bscscan_top_holders(addr, api_key, offset=10)
            if top_holders:
                top10_total = 0
                for h in top_holders:
                    balance = int(h.get("TokenHolderQuantity", 0) or 0)
                    holder_addr = (h.get("TokenHolderAddress") or "").lower()
                    # 排除零地址/死地址/合约地址 (这些不算真实持仓)
                    if holder_addr in BURN_ADDRESSES:
                        continue
                    top10_total += balance
                if total_supply > 0:
                    concentration = top10_total / total_supply
                    t["top10Concentration"] = round(concentration, 4)
                    if concentration > QUALITY_MAX_TOP10_CONCENTRATION:
                        exclude_reason = "Top10持仓{:.0f}% (庄家控盘)".format(concentration * 100)
        except Exception as e:
            log.debug("Top Holder 查询失败 %s: %s", name, e)

        # 2. 开发者行为检查 (仅有 creator 地址时)
        if not exclude_reason and creator:
            try:
                dev = analyze_developer_behavior(addr, creator, api_key)
                if dev.get("exclude"):
                    exclude_reason = "开发者异常: " + ", ".join(dev.get("details", []))
                t["devBehavior"] = {
                    "hasSell": dev.get("has_sell", False),
                    "hasBuy": dev.get("has_buy", False),
                    "hasLpRemove": dev.get("has_lp_remove", False),
                    "sellPct": dev.get("sell_pct", 0),
                }
            except Exception as e:
                log.debug("开发者行为查询失败 %s: %s", name, e)

        # 3. 假K线检测 (GeckoTerminal 15min + 1min K线)
        # 特征 A/B: 15min K线全是实体柱无影线 / 全阳线 = 价格被控制
        # 特征 C: 1min K线头部脉冲 + 后续全是死线 = 拉盘后无人接盘
        if not exclude_reason:
            try:
                candles_15m = gt_ohlcv_15min(addr, limit=12)  # 最近 3 小时的 15min K线
                candles_1m = gt_ohlcv_1min(addr, limit=30)    # 最近 30 根 1min K线
                fake_result = detect_fake_candles(candles_15m, candles_1m)
                t["fakeCandle"] = fake_result
                if fake_result["fake"]:
                    exclude_reason = "假K线 ({} — 控盘刷量)".format(fake_result["reason"])
            except Exception as e:
                log.debug("假K线检测失败 %s: %s", name, e)

        if exclude_reason:
            log.info("  精筛后防线: ✗ %s — %s", name, exclude_reason)
            t["defenseExcludeReason"] = exclude_reason
        else:
            results.append(t)

    excluded = len(candidates) - len(results)
    if excluded > 0:
        log.info("精筛后防线: 排除 %d 个, 通过 %d 个", excluded, len(results))

    return results


# ===================================================================
#  数据文件清理 — 删除 48 小时前的 JSON 文件
# ===================================================================
def cleanup_old_data(max_age_days: int = 2):
    """删除 data/ 目录下超过 max_age_days 天的 JSON 文件 (排除 queue.json, smart_money.json)
    通过文件名中的时间戳判断, 而非 mtime (GitHub Actions checkout 后 mtime 为当前时间)
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    count = 0
    for f in DATA_DIR.glob("*.json"):
        if f.name in ("queue.json", "smart_money.json"):
            continue
        # 从文件名解析时间: 2026-04-10T12-34-56.json → 2026-04-10T12:34:56
        try:
            parts = f.stem.split("T")
            if len(parts) != 2:
                continue
            date_part = parts[0]              # 2026-04-10 (已是合法格式)
            time_part = parts[1].replace("-", ":")  # 12-34-56 → 12:34:56
            file_dt = datetime.fromisoformat(f"{date_part}T{time_part}").replace(tzinfo=timezone.utc)
            if file_dt < cutoff:
                f.unlink()
                count += 1
        except (ValueError, OSError):
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

    # 计时开始
    _t_start = time.time()

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
                "addedPrice": detail["price"],
                "peakPrice": detail["price"],
                "holders": detail["holders"],
                "addedHolders": detail["holders"],
                "peakHolders": detail["holders"],
                "liquidity": detail.get("liquidity", 0),
                "peakLiquidity": detail.get("liquidity", 0),
                "raisedAmount": detail.get("raisedAmount", 0),
                "progress": detail.get("progress", 0),
                "peakProgress": detail.get("progress", 0),
                "addedProgress": detail.get("progress", 0),
                "day1Vol": detail.get("day1Vol", 0),
                "consecDrops": 0,
                "lastPrice": detail["price"],
                "priceHistory": [detail["price"]],
                "holdersHistory": [detail["holders"]],
            })

    log.info("入队后: %d 个代币", len(queue_state["tokens"]))

    # Step 3: 淘汰检查 (v6: 返回 survivors, eliminated, breakthrough)
    log.info("\n--- Step 3: 淘汰检查 ---")
    survivors, eliminated, breakthrough_tokens = elimination_check(queue_state["tokens"], now_ms, bscscan_key)
    queue_state["tokens"] = survivors
    queue_state["eliminated"].extend([{
        "address": e["address"], "name": e.get("name", ""),
        "symbol": e.get("symbol", ""),
        "elimReason": e["elimReason"], "eliminatedAt": e["eliminatedAt"],
        "createdAt": e.get("createdAt", 0),
    } for e in eliminated])

    # 突破代币持币数刷新: 冻结快照前用 BSCScan 爬取真实持币数
    # 原因: 代币刚毕业时 detail API 返回 holders=0, 导致快照持币数为 0
    if breakthrough_tokens:
        bt_addrs = [t["address"] for t in breakthrough_tokens]
        log.info("突破代币持币数刷新: BSCScan 查询 %d 个代币...", len(bt_addrs))
        bt_holders = graduated_holder_counts(bt_addrs)
        for t in breakthrough_tokens:
            rh = bt_holders.get(t["address"])
            if rh is not None and rh > 0:
                old_h = t.get("holders", 0)
                t["holders"] = rh
                t["peakHolders"] = max(t.get("peakHolders", 0), rh)
                if rh != old_h:
                    log.info("  突破持币数刷新 %s: %d→%d (BSCScan)",
                             t.get("name") or t["address"][:16], old_h, rh)
            elif t.get("holders", 0) == 0 and t.get("peakHolders", 0) > 0:
                # BSCScan 也没查到, 用 peakHolders 兜底 (总比 0 好)
                t["holders"] = t["peakHolders"]
                log.info("  突破持币数兜底 %s: 0→%d (用 peakHolders)",
                         t.get("name") or t["address"][:16], t["peakHolders"])

    # 价格突破的代币记入 breakthrough 持久化列表 (冻结快照, 不再跟踪)
    if "breakthrough" not in queue_state:
        queue_state["breakthrough"] = []
    queue_state["breakthrough"].extend([{
        "address": t["address"], "name": t.get("name", ""),
        "symbol": t.get("symbol", ""),
        "breakthroughAt": t.get("breakthroughAt", now_ms),
        "createdAt": t.get("createdAt", 0),
        "peakPrice": t.get("peakPrice", 0),
        "price": t.get("price", 0),
        "holders": t.get("holders", 0),
        "peakHolders": t.get("peakHolders", 0),
        "liquidity": t.get("liquidity", 0),
        "progress": t.get("progress", 0),
        "socialCount": t.get("socialCount", 0),
        "socialLinks": t.get("socialLinks", {}),
        "raisedAmount": t.get("raisedAmount", 0),
    } for t in breakthrough_tokens])

    # 历史突破代币持币数补查: 已冻结的突破代币如果 holders=0, 用 BSCScan 补一次
    zero_bt = [t for t in queue_state.get("breakthrough", []) if t.get("holders", 0) == 0]
    if zero_bt:
        zb_addrs = [t["address"] for t in zero_bt]
        log.info("历史突破持币数补查: %d 个代币 holders=0, BSCScan 查询...", len(zero_bt))
        zb_holders = graduated_holder_counts(zb_addrs)
        fixed = 0
        for t in zero_bt:
            rh = zb_holders.get(t["address"])
            if rh is not None and rh > 0:
                t["holders"] = rh
                t["peakHolders"] = max(t.get("peakHolders", 0), rh)
                fixed += 1
                log.info("  历史突破持币数修复 %s: 0→%d (BSCScan)",
                         t.get("name") or t["address"][:16], rh)
            elif t.get("peakHolders", 0) > 0:
                t["holders"] = t["peakHolders"]
                fixed += 1
                log.info("  历史突破持币数兜底 %s: 0→%d (用 peakHolders)",
                         t.get("name") or t["address"][:16], t["peakHolders"])
        if fixed:
            log.info("历史突破持币数补查: 修复 %d/%d 个", fixed, len(zero_bt))

    # 入场被拒的代币也记入 eliminated (避免重复入场筛)
    queue_state["eliminated"].extend([{
        "address": r["token"].get("address", ""),
        "name": (r.get("detail") or {}).get("name") or r["token"].get("name", ""),
        "symbol": (r.get("detail") or {}).get("shortName") or r["token"].get("symbol", ""),
        "elimReason": f"入场拒绝: {r.get('reason', '')}",
        "eliminatedAt": now_ms,
        "createdAt": r["token"].get("createdAt", 0),
    } for r in rejected_at_entry if r["token"].get("address")])

    log.info("淘汰后: %d 个存活, %d 个淘汰, %d 个突破", len(survivors), len(eliminated), len(breakthrough_tokens))

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

    # 精筛 (动量筛选, 从存活币中找起飞信号)
    # 冷却期: 从 queue_state 恢复, 单次执行需持久化
    cooldown_map = queue_state.get("qualityCooldown", {})
    scan_round = queue_state.get("scanRound", 0) + 1
    quality_results = quality_filter(survivors, now_ms,
                                     cooldown_map=cooldown_map,
                                     current_round=scan_round)
    # 持久化冷却期状态
    queue_state["qualityCooldown"] = cooldown_map
    queue_state["scanRound"] = scan_round

    # 精筛代币持币数刷新: 用 BSCScan 网页爬取真实持币数 (four.meme detail 对未毕业币不准)
    # 必须在再验证之前执行, 否则再验证用的是旧数据
    if quality_results:
        q_addrs = [t["address"] for t in quality_results]
        log.info("精筛持币数刷新: BSCScan 查询 %d 个代币...", len(q_addrs))
        real_holders = graduated_holder_counts(q_addrs)
        for t in quality_results:
            rh = real_holders.get(t["address"])
            if rh is not None and rh > 0:
                old_h = t.get("holders", 0)
                t["holders"] = rh
                t["peakHolders"] = max(t.get("peakHolders", 0), rh)
                # 同步更新队列中的对应代币
                for s in survivors:
                    if s["address"] == t["address"]:
                        s["holders"] = rh
                        s["peakHolders"] = max(s.get("peakHolders", 0), rh)
                        break
                if rh != old_h:
                    log.info("  持币数刷新 %s: %d→%d (BSCScan)",
                             t.get("name") or t["address"][:16], old_h, rh)

    # 精筛后防线: Top Holder 集中度 + 开发者行为检查
    if quality_results:
        quality_results = post_quality_defense(quality_results, bscscan_key)

    # 精筛再验证: 对精筛结果重新检查淘汰条件
    # - 不符合队列存活 → 移到本轮淘汰
    # - 符合队列存活但不符合精筛 → 留在队列 (从精筛列表移除)
    revalidated = []
    demoted_to_elim = 0
    for t in quality_results:
        # 检查是否满足淘汰条件 (复用 elimination_check 的条件逻辑)
        age_hours = (now_ms - t.get("createdAt", 0)) / 3600000
        current_price = t.get("price", 0)
        current_holders = t.get("holders", 0)
        current_liq = t.get("liquidity", 0)
        current_progress = t.get("progress", 0)
        peak_price = t.get("peakPrice", 0)
        is_graduated = current_progress >= 1
        elim_reason = None

        # 币龄淘汰
        if age_hours > MAX_AGE_HOURS:
            elim_reason = f"币龄>{MAX_AGE_HOURS}h"
        # 价格跌 90%
        if not elim_reason and peak_price > 0 and current_price > 0:
            if current_price < peak_price * (1 - ELIM_PRICE_DROP_PCT):
                elim_reason = (f"价格跌{(1 - current_price / peak_price) * 100:.0f}% "
                               f"(峰:{peak_price:.2e} 现:{current_price:.2e})")
        # 持币数从 30+ 跌破 10
        if not elim_reason:
            if (t.get("peakHolders", 0) >= ELIM_HOLDERS_PEAK_MIN
                    and current_holders < ELIM_HOLDERS_FLOOR):
                elim_reason = f"持币数 {t.get('peakHolders', 0)}→{current_holders}"
        # 无社交
        if not elim_reason and t.get("socialCount", 0) < MIN_SOCIAL_COUNT:
            elim_reason = "无社交媒体"
        # 流动性枯竭 (仅已毕业)
        if not elim_reason and is_graduated:
            if (t.get("peakLiquidity", 0) >= ELIM_LIQ_PEAK_MIN
                    and current_liq < ELIM_LIQ_FLOOR):
                elim_reason = f"流动性 ${t.get('peakLiquidity', 0):.0f}→${current_liq:.0f}"
        # 进度淘汰
        if not elim_reason:
            if age_hours > ELIM_PROGRESS_AGE_HOURS and current_progress < ELIM_PROGRESS_MIN:
                elim_reason = f"进度{current_progress * 100:.2f}% 币龄{age_hours:.1f}h"
        if not elim_reason:
            if age_hours > ELIM_PROGRESS_AGE_HOURS_MID and current_progress < ELIM_PROGRESS_MIN_MID:
                elim_reason = f"进度{current_progress * 100:.2f}% 币龄{age_hours:.1f}h"
        # 早期/中期持币数不足
        if not elim_reason:
            if age_hours > ELIM_EARLY_AGE_MIN and t.get("peakHolders", 0) < ELIM_EARLY_PEAK_HOLDERS:
                elim_reason = f"币龄{age_hours:.1f}h 最高持币仅{t.get('peakHolders', 0)}"
        if not elim_reason:
            if age_hours > ELIM_MID_AGE_HOURS and t.get("peakHolders", 0) < ELIM_MID_PEAK_HOLDERS:
                elim_reason = f"币龄{age_hours:.1f}h 最高持币仅{t.get('peakHolders', 0)}"

        if elim_reason:
            # 从队列存活中移除, 加入淘汰列表
            survivors[:] = [s for s in survivors if s["address"] != t["address"]]
            eliminated.append({**t, "eliminatedAt": now_ms, "elimReason": elim_reason})
            queue_state["eliminated"].append({
                "address": t["address"], "name": t.get("name", ""),
                "symbol": t.get("symbol", ""),
                "elimReason": elim_reason, "eliminatedAt": now_ms,
                "createdAt": t.get("createdAt", 0),
            })
            demoted_to_elim += 1
            log.info("精筛再验证: ✗ %s → 淘汰 (%s)",
                     t.get("name") or t["address"][:16], elim_reason)
        else:
            revalidated.append(t)

    # 再验证后的精筛结果替换原列表
    quality_results = revalidated

    if demoted_to_elim > 0:
        log.info("精筛再验证: %d 个淘汰", demoted_to_elim)

    # 按持币数排序
    quality_results.sort(key=lambda x: (x.get("holders", 0)), reverse=True)

    log.info("精筛通过: %d/%d", len(quality_results), len(survivors))

    # 更新队列状态 (精筛再验证可能修改了 survivors)
    queue_state["tokens"] = survivors
    queue_state["lastBlock"] = latest_block
    queue_state["lastScanTime"] = now_ms
    save_queue(queue_state)

    # --- 构建输出 JSON ---
    from datetime import timedelta
    now_dt = datetime.now(timezone(timedelta(hours=8)))  # 北京时间
    scan_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    file_ts = now_dt.strftime("%Y-%m-%dT%H-%M-%S")

    # tokens: 精筛结果
    out_tokens = []
    for t in quality_results:
        cc = t.get("copycat", {})
        _price = t.get("price", 0)
        _peak = t.get("peakPrice", 0)
        _ratio = _price / _peak if _peak > 0 and _price > 0 else None
        out_tokens.append({
            "address": t.get("address", ""),
            "name": t.get("name", ""),
            "symbol": t.get("symbol", ""),
            "holders": t.get("holders", 0),
            "peak_holders": t.get("peakHolders", 0),
            "created_at": t.get("createdAt", 0),
            "total_supply": t.get("totalSupply", 0),
            "price": _price,
            "max_price": _peak,
            "price_ratio": _ratio,
            "age_hours": round((now_ms - t.get("createdAt", 0)) / 3600000, 2),
            "social_count": t.get("socialCount", 0),
            "social_links": t.get("socialLinks", {}),
            "copycat_count": cc.get("count", 0),
            "is_copycat": cc.get("isCopycat", False),
            "day1_vol": t.get("day1Vol", 0),
            "progress": t.get("progress", 0),
            "liquidity": t.get("liquidity", 0),
            "peak_liquidity": t.get("peakLiquidity", 0),
            "raised_amount": t.get("raisedAmount", 0),
            "volume_24h": t.get("volume24h", 0),
            "volume_h1": t.get("volumeH1", 0),
            "buys_h1": t.get("buysH1", 0),
            "sells_h1": t.get("sellsH1", 0),
            "buys_h24": t.get("buysH24", 0),
            "sells_h24": t.get("sellsH24", 0),
            "top10_concentration": t.get("top10Concentration"),
            "dev_behavior": t.get("devBehavior"),
            "is_rocket": t.get("isRocket", False),
            "is_graduated": t.get("isGraduated", False),
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

    # breakthroughTokens: 本轮新突破 + 历史突破 (从 queue_state 累积)
    out_breakthrough = []
    # 本轮新突破
    for t in breakthrough_tokens:
        out_breakthrough.append({
            "address": t.get("address", ""),
            "name": t.get("name", ""),
            "symbol": t.get("symbol", ""),
            "holders": t.get("holders", 0),
            "peak_holders": t.get("peakHolders", 0),
            "created_at": t.get("createdAt", 0),
            "price": t.get("price", 0),
            "peak_price": t.get("peakPrice", 0),
            "liquidity": t.get("liquidity", 0),
            "progress": t.get("progress", 0),
            "social_count": t.get("socialCount", 0),
            "social_links": t.get("socialLinks", {}),
            "raised_amount": t.get("raisedAmount", 0),
            "age_hours": round((now_ms - t.get("createdAt", 0)) / 3600000, 2),
            "breakthrough_at": t.get("breakthroughAt", now_ms),
        })
    # 历史突破 (从 queue_state 累积, 不含本轮)
    for t in queue_state.get("breakthrough", []):
        # 跳过本轮已输出的
        if any(b["address"] == t["address"] for b in out_breakthrough):
            continue
        out_breakthrough.append({
            "address": t.get("address", ""),
            "name": t.get("name", ""),
            "symbol": t.get("symbol", ""),
            "holders": t.get("holders", 0),
            "peak_holders": t.get("peakHolders", 0),
            "created_at": t.get("createdAt", 0),
            "price": t.get("price", 0),
            "peak_price": t.get("peakPrice", 0),
            "liquidity": t.get("liquidity", 0),
            "progress": t.get("progress", 0),
            "social_count": t.get("socialCount", 0),
            "social_links": t.get("socialLinks", {}),
            "raised_amount": t.get("raisedAmount", 0),
            "age_hours": round((now_ms - t.get("createdAt", 0)) / 3600000, 2),
            "breakthrough_at": t.get("breakthroughAt", 0),
        })

    output = {
        "scanTime": scan_time_str,
        "totalTokens": len(new_on_chain),
        "newDiscovered": len(new_on_chain),
        "newAdmitted": len(admitted),
        "eliminatedCount": len(eliminated),
        "breakthroughCount": len(breakthrough_tokens),
        "filteredTokens": len(quality_results),
        "queueSize": len(survivors),
        "tokens": out_tokens,
        "queue": out_queue,
        "breakthroughTokens": out_breakthrough,
        "eliminatedThisRound": out_eliminated,
        "rejectedAtEntry": out_rejected,
    }

    # 写入 data/{timestamp}.json
    out_path = DATA_DIR / f"{file_ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info("输出: %s", out_path)

    # 清理过期数据文件
    cleanup_old_data(2)

    # 打印摘要
    log.info("=" * 50)
    log.info("扫描完成")
    log.info("  链上发现: %d", len(new_on_chain))
    log.info("  入场通过: %d, 入场淘汰: %d", len(admitted), len(rejected_at_entry))
    log.info("  队列淘汰: %d, 价格突破: %d, 队列存活: %d", len(eliminated), len(breakthrough_tokens), len(survivors))
    log.info("  精筛通过: %d", len(quality_results))
    log.info("  总耗时: %.1f 秒", time.time() - _t_start)
    log.info("=" * 50)


if __name__ == "__main__":
    main()
