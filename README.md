# BSC Token Scanner

BSC 链上新代币扫描器。直接扫描链上 [Four.meme](https://four.meme) 和 [Flap](https://flap.sh/bnb) 合约的 `TokenCreated` 事件发现新代币，采用**队列淘汰制**持续跟踪，自动剔除弃盘币，对存活代币执行潜伏型精筛 + 精筛后防线深度检查。

与姊妹项目 `token_trading` 共用同一套筛选策略，由外部 cron（GitHub Actions）每 15 分钟触发一次，单次执行，输出 JSON 到 `data/` 目录供前端展示。

## v6 架构：极速扫描 + 精筛后防线

```
每 15 分钟执行一次:

1. 链上发现 (~1s)
   BSC RPC eth_getLogs → four.meme + flap TokenCreated 事件 → 新代币地址

2. 入场筛 (~数秒)
   four.meme Detail API + flap.sh 页面 SSR 社交数据 + 链上 totalSupply
   → 淘汰无社交 / 总量≠10亿 / 币龄>48h

3. 淘汰检查 (~数秒)
   DexScreener 批量查价(含交易量/买卖笔数) + GeckoTerminal 持币数 + Detail API
   → 永久淘汰弃盘币

4. 精筛 (瞬时)
   潜伏型筛选: 持币≥50 + 进度30%~90% + 币龄≤10h + 没在崩盘 + 仿盘≥3
   → 从存活币中找蓄势待发信号

5. 毕业通道 (瞬时)
   刚毕业强势币: 持币≥100 + 流动性≥$10k + 币龄≤24h + 没在崩盘
   → 从存活币中找刚毕业的强势代币

6. 仿盘检测
   本地统计同名代币数量 (零 API 调用)
```

## 代币来源

两个平台都使用 bonding curve 机制，买走 80% 供应量后迁移到 PancakeSwap。

| 平台 | 合约 | 代币后缀 | Detail API |
|------|------|----------|------------|
| four.meme | `0x5c952063...` (TokenManagerOriginal) | `4444` / `ffff` | ✅ four.meme Detail API |
| flap | `0xe2cE6ab0...` (Portal) | `8888` / `7777` | ❌ 无 Detail API (进度通过链上 `getToken()` 读取) |

## 数据源

| 数据源 | 用途 | 限流 |
|--------|------|------|
| BSC RPC (publicnode) | 链上 TokenCreated 事件发现 + flap getToken() 进度查询 | 无硬限制 |
| four.meme Detail API | 社交链接/持币数(bonding curve阶段)/进度/募资额 | ~5 req/s |
| flap.sh 页面 SSR | flap 代币社交媒体 (twitter/telegram/website) | ~5 req/s |
| DexScreener API | 批量价格+流动性+交易量+买卖笔数 | ~300 req/min |
| GeckoTerminal Token Info | 持币地址数 (已毕业代币, 链上索引) | ~30 req/min |
| 本地队列统计 | 仿盘检测：同名/近似名代币数量 | 无 |

### 目标价区间

目标价区间: 0.000001 ~ 0.0001。尽可能低价买入，等突破 0.0001 卖出。

峰值价格突破 0.0001 的代币标记为「已突破」，保留在队列中继续跟踪更新，跳过常规淘汰条件（价格跌幅、持币数下降等），仅受币龄 >48h 淘汰。已突破代币同时出现在队列存活和已突破 tab 中，便于单独查看和统计分析。

峰值价格（peakPrice）是代币在队列存活期间记录到的最高价格（每轮用 DexScreener 实时价取 max），用于淘汰判断（价格跌 90% 淘汰）。

### 持币数查询方案

持币数是筛选和淘汰的核心指标，但 four.meme 代币的生命周期跨越 bonding curve 和 DEX 两个阶段，没有单一数据源能覆盖全程。当前采用多源互补策略：

**优先级: GeckoTerminal > BSCScan 网页爬取 > four.meme Detail API > 缓存**

| 数据源 | 覆盖阶段 | 说明 |
|--------|----------|------|
| GeckoTerminal `/tokens/{addr}/info` | 已毕业 (DEX 阶段) | 免费、无需 key，链上索引数据，最准确 |
| BSCScan 网页爬取 | 已毕业 (DEX 阶段) | GT 查不到时的降级备选 |
| four.meme Detail API `holderCount` | Bonding curve 阶段 | 平台内部记账，毕业后返回 0 |
| 队列缓存 | 兜底 | 上一轮的持币数，避免数据断档 |

注意：只对已毕业代币（progress ≥ 1）发起 GT/BSCScan 查询，未毕业代币直接用 detail API，避免浪费请求。

**已验证不可用的方案及原因：**

| 方案 | 问题 |
|------|------|
| BSCScan `tokenholdercount` API | 免费 API key 不支持 BSC 链 |
| RPC `eth_getLogs` Transfer 事件 | Bonding curve 阶段不产生标准 ERC-20 Transfer 事件 |
| DexScreener API | 不返回持币地址数字段 |
| CoinGecko 免费 API | 小代币未收录 (404) |

## 淘汰规则（永久剔除）

满足任一条件即从队列中永久移除：

| # | 条件 | 说明 |
|---|------|------|
| 1 | 价格从峰值跌 90%+ | 暴跌弃盘 (当前价格<1e-7 视为 API 异常, 跳过) |
| 2 | 持币地址从 ≥30 跌破 10 | 大量抛售 |
| 3 | 持币数从峰值跌 70%+ (峰值≥50) | 僵尸币清理 |
| 4 | 无社交媒体 | 无运营意愿 (four.meme 通过 Detail API, flap 通过 flap.sh 页面 SSR 提取, 统一淘汰) |
| 5 | 流动性从 >$1k 跌破 $100 (仅已毕业) | 流动性枯竭 |
| 6 | 进度 < 1% 且币龄 > 2h | bonding curve 上的死币 |
| 6b | 进度 < 5% 且币龄 > 4h | 进度停滞 |
| 7 | 进度从峰值跌 50%+ 且币龄 > 6h | 热度消退 (峰值持币≥50 的社区币放宽到 70%) |
| 8 | 币龄 > 15min 且最高持币数 < 3 | 无人问津 |
| 9 | 币龄 > 1h 且最高持币数 < 5 | 热度不足 |
| 9b | 币龄 > 2h 且最高持币数 < 8 | 僵尸币清理 |
| 10 | 币龄 > 48h | 超出关注窗口 |
| 🚀 | 价格突破: 峰值价格 ≥ 0.0001 | 标记为已突破, 跳过常规淘汰条件, 仅受币龄淘汰 |
| 🔄 | 突破 flap 刷新: 每轮强制重新拉取数据 | 重算 peakPrice, 不达标则取消突破标记 |

## 精筛规则（潜伏型筛选）

从队列存活币中找"蓄势待发"的代币，核心思路：不追涨，在代币有社区基础 + 有资金 + 没在崩盘时潜伏进去。

仅未毕业币，已毕业币不走此通道（毕业后价格波动大，潜伏逻辑不适用）。条件全部满足（AND）：

| 条件 | 阈值 | 说明 |
|------|------|------|
| 持币数 | ≥ 50 | 有真实社区 |
| 进度 | 30% ~ 90% | 有真金白银，还有上涨空间 |
| 币龄 | ≤ 10h | 还年轻 |
| 近 1 轮持币变化 | ≥ -5 | 没在大量流失 |
| 近 1 轮价格变化 | ≥ -20% | 没在暴跌 |
| 仿盘数 | ≥ 3 | 有市场热度, 冷门题材排除 |

## 毕业通道（刚毕业强势币）

从队列存活币中找刚毕业的强势代币，与潜伏型精筛互补。核心思路：毕业后短暂窗口期，有流动性 + 社区还在 + 没在崩盘时快速介入。

仅已毕业币（progress >= 1.0）。条件全部满足（AND）：

| 条件 | 阈值 | 说明 |
|------|------|------|
| 持币数 | ≥ 100 | 毕业后需要更强社区支撑 |
| 流动性 | ≥ $10,000 | 有真实交易深度 |
| 币龄 | ≤ 24h | 毕业后窗口期短 |
| 近 1 轮持币变化 | ≥ 0 | 持币数不能在减少 |
| 近 1 轮价格变化 | ≥ 0% | 必须还在涨，毕业后首轮跌的基本没戏 |

## 待启用的数据源（已实现未接入）

代码中已实现但扫描主流程未调用的函数和数据，后续可接入以提升筛选准确率。

> 注意：币安 Web3 API 可能禁止美国 IP 访问。本项目运行在 GitHub Actions（美国服务器），需验证可用性后再决定是否接入。姊妹项目 `token_trading`（韩国首尔服务器）已确认可用。

### 币安 Web3 Token Dynamic API (`fetch_binance_token_dynamic`)

对精筛后防线价值最大的数据源，单次调用即可获取以下字段：

| 字段 | 含义 | 潜在用途 |
|------|------|----------|
| `top10HoldersPercentage` | Top10 持仓占比 | 替代当前失效的 BSCScan `tokenholderlist`，判断庄家控盘 |
| `devHoldingPercent` | 开发者持仓占比 | 开发者持仓过高=跑路风险，过低=已清仓 |
| `smartMoneyHolders` | 聪明钱持仓人数 | 聪明钱在买=正向信号 |
| `smartMoneyHoldingPercent` | 聪明钱持仓占比 | 聪明钱重仓=高置信度 |
| `kolHolders` | KOL 持仓人数 | KOL 关注=有传播潜力 |
| `kolHoldingPercent` | KOL 持仓占比 | KOL 重仓=强信号 |
| `proHolders` | 专业交易者人数 | 专业玩家在场=非纯散户盘 |
| `proHoldingPercent` | 专业交易者持仓占比 | 专业玩家重仓=值得跟 |
| `percentChange1h` | 1h 涨跌幅 | 短期动量参考 |
| `percentChange24h` | 24h 涨跌幅 | 中期趋势参考 |

当前问题：精筛后防线的 Top10 检查用 BSCScan `tokenholderlist` API，但 four.meme 未毕业代币不产生标准 ERC20 Transfer，BSCScan 索引不到 holder 列表，导致 `top10_concentration` 全部为 null（115 条精筛记录无一有值）。用币安 Token Dynamic API 可直接解决。

### 币安聪明钱信号 (`fetch_binance_smart_signals`)

| 字段 | 含义 | 潜在用途 |
|------|------|----------|
| `direction` | 买入/卖出方向 | 聪明钱正在买入=强正向信号 |
| `smartMoneyCount` | 聪明钱数量 | 多个聪明钱同时买入=高置信度 |
| `exitRate` | 退出率 | 退出率高=风险信号 |
| `maxGain` | 最大收益 | 历史收益参考 |
| `tagEvents` | 敏感事件标签 | 风险预警（如 rug pull 标记） |

### 钱包分析 (`batch_wallet_analysis`)

整合开发者行为 + 币安信号 + 聪明钱匹配的完整分析流程，已实现但未在扫描主流程中调用。

### DexScreener 未使用字段

当前 `ds_batch_prices` 已提取价格/流动性/交易量/买卖笔数，但 DexScreener API 还返回了以下字段被丢弃：

| 字段 | 含义 | 潜在用途 |
|------|------|----------|
| `priceChange` (m5/h1/h6/h24) | 各时间段涨跌幅 | 多时间维度动量判断，比自己算更准 |
| `fdv` | 完全稀释估值 | 估值过高的币利润空间小 |
| `marketCap` | 市值 | 市值筛选 |
| `boosts.active` | 是否有付费推广 | 有推广=项目方在花钱运营，正向信号 |
| `info.socials` | 社交媒体列表 | 补充 four.meme 的社交数据 |

### 优先级建议

1. 币安 Token Dynamic → 替换 BSCScan Top Holder（解决 top10 数据缺失问题，同时获得聪明钱/KOL/开发者持仓）
2. DexScreener `priceChange` → 零额外 API 调用，直接从现有响应中提取
3. 币安聪明钱信号 → 作为精筛加分项或防线加固
4. DexScreener `boosts` → 零额外调用，项目方付费推广是正向信号

### 开发者行为判定

- 开发者地址来源：链上 `TokenCreated` 事件解码的 `creator` 字段
- 转账查询：BSCScan `tokentx` 查开发者对该代币的转账记录
- 买入判定：从 DEX Router（PancakeSwap V2/V3/Universal）或零地址收到代币
- 卖出判定：转到 DEX Router 或其他非自己地址（转到零地址/死地址视为销毁，不算卖出）
- 清仓判定：卖出占收到总量 ≥ 90%
- 流动性操作：通过 LP token 的 mint/burn 检测（from=0x0 → 加池子，to=0x0 → 撤池子）

## 前端功能

四个 Tab 视图：

- **精筛结果**：通过全部筛选条件的推荐代币（含买卖比、交易量、Top10集中度、开发者行为等新指标）
- **队列存活**：当前队列中所有存活代币（含价格/持币/流动性/峰值等）
- **本轮淘汰**：本轮被淘汰的代币及淘汰原因
- **入场淘汰**：新发现但未通过入场筛的代币及原因（无社交/总量不符等）

搜索功能覆盖全部四个 Tab 的历史数据，任何代币都可以搜到并查看淘汰原因。用合约地址搜索时，会展示该代币在所有历史扫描时间点的快照（价格、持币数、流动性等变化），方便追踪代币的完整生命周期。

## 使用方法

```bash
npm run scan                    # 执行一次扫币（链上发现 + 队列淘汰 + 精筛 + 防线）
npm run build                   # 构建静态站点
npm run scan && npm run build   # 扫币 + 构建（完整流程）
npm run dev                     # 启动开发服务器（live-server）
```

`npm run scan` 即 `python3 scripts/scan.py`，执行一次完整扫描流程。扫描结果保存在 `data/` 目录（保留近 48 小时），队列状态持久化在 `data/queue.json`。GitHub Actions 每 15 分钟自动执行一次。

## 配置

`config.local.json`（已 gitignore）：

```json
{
  "proxy": { "enabled": true, "host": "127.0.0.1", "port": 7890 },
  "bscscanApiKey": "YOUR_BSCSCAN_API_KEY"
}
```

`scripts/scan.py` 顶部常量：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_AGE_HOURS` | 48 | 关注窗口（小时） |
| `SCAN_INTERVAL_MIN` | 15 | 扫描间隔（分钟） |
| `QUALITY_MIN_HOLDERS` | 50 | 精筛: 持币数 ≥ 50 (有真实社区) |
| `QUALITY_MIN_PROGRESS` | 0.30 | 精筛: 进度 ≥ 30% (有真金白银) |
| `QUALITY_MAX_PROGRESS` | 0.90 | 精筛: 进度 < 90% (还有上涨空间) |
| `QUALITY_MAX_AGE_HOURS` | 10 | 精筛: 币龄 ≤ 10h (还年轻) |
| `QUALITY_MIN_H_DELTA` | -5 | 精筛: 近 1 轮持币变化 ≥ -5 (没在大量流失) |
| `QUALITY_MIN_PRICE_CHANGE` | -0.20 | 精筛: 近 1 轮价格变化 ≥ -20% (没在暴跌) |
| `GRAD_MIN_HOLDERS` | 100 | 毕业通道: 持币数 ≥ 100 |
| `GRAD_MIN_LIQUIDITY` | 10000 | 毕业通道: 流动性 ≥ $10,000 |
| `GRAD_MAX_AGE_HOURS` | 24 | 毕业通道: 币龄 ≤ 24h |
| `GRAD_MIN_H_DELTA` | 0 | 毕业通道: 近 1 轮持币变化 ≥ 0 |
| `GRAD_MIN_PRICE_CHANGE` | 0.0 | 毕业通道: 近 1 轮价格变化 ≥ 0% |
| `GRAD_STOP_LOSS_PCT` | -30 | 毕业通道: 止损 -30% |
| `ELIM_PRICE_DROP_PCT` | 0.90 | 价格跌幅淘汰阈值 |
| `ELIM_HOLDERS_FLOOR` | 10 | 持币数淘汰下限 |
| `ELIM_LIQ_FLOOR` | 100 | 流动性淘汰下限（USD） |
| `ELIM_EARLY_PEAK_HOLDERS` | 3 | 币龄>15min 最高持币数淘汰下限 |
| `ELIM_MID_PEAK_HOLDERS` | 5 | 币龄>1h 最高持币数淘汰下限 |
| `ELIM_LATE_PEAK_HOLDERS` | 8 | 币龄>2h 最高持币数淘汰下限 |
| `ELIM_PROGRESS_DROP_PCT` | 0.50 | 进度跌幅淘汰阈值 |
| `ELIM_PROGRESS_DROP_PCT_RELAXED` | 0.70 | 进度跌幅淘汰阈值 (峰值持币≥50 的社区币) |
| `ELIM_PRICE_DROP_MIN_PRICE` | 1e-7 | 价格暴跌保护: 低于此值视为 API 异常 |

## 项目结构

```
├── .github/workflows/
│   └── scan.yml              # GitHub Actions 定时任务
├── data/
│   ├── queue.json            # 队列状态（代币列表 + 已淘汰记录 + lastBlock）
│   ├── smart_money.json      # 聪明钱地址缓存
│   └── 2026-04-14T*.json     # 每轮扫描结果（含精筛/队列/淘汰快照）
├── scripts/
│   └── scan.py               # 扫描主脚本（链上发现 + 队列淘汰 + 精筛 + 防线）
├── public/
│   └── index.html            # 前端页面源文件
├── site/                     # 构建产物（GitHub Pages）
│   ├── index.html
│   └── data/
├── config.local.json         # 本地配置（已 gitignore）
└── package.json
```

## 跨项目同步

本项目与 `token_trading` 共用同一套筛选策略。两个项目的以下部分必须保持一致：

- 顶部常量区（所有 `QUALITY_*`、`ELIM_*` 阈值）
- 核心函数：`discover_on_chain`、`admission_filter`、`elimination_check`、`quality_filter`、`graduated_quality_filter`、`post_quality_defense`
- 数据源函数：`ds_batch_prices`、`graduated_holder_counts` 等
- 文件头注释中的条件描述

## License

ISC
