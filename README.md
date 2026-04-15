# BSC Token Scanner

BSC 链上新代币扫描器。直接扫描链上 [Four.meme](https://four.meme) 合约的 `TokenCreated` 事件发现新代币，采用**队列淘汰制**持续跟踪，自动剔除弃盘币，对存活代币执行动量精筛 + 精筛后防线深度检查。

与姊妹项目 `token_trading` 共用同一套筛选策略，由外部 cron（GitHub Actions）每 15 分钟触发一次，单次执行，输出 JSON 到 `data/` 目录供前端展示。

## v6 架构：极速扫描 + 精筛后防线

```
每 15 分钟执行一次:

1. 链上发现 (~1s)
   BSC RPC eth_getLogs → four.meme TokenCreated 事件 → 新代币地址

2. 入场筛 (~数秒)
   four.meme Detail API → 淘汰无社交 / 总量≠10亿 / 币龄>48h

3. 淘汰检查 (~数秒)
   DexScreener 批量查价(含交易量/买卖笔数) + GeckoTerminal 持币数 + Detail API
   → 永久淘汰弃盘币

4. 精筛 (瞬时)
   动量筛选: 价格加速度 + 持币增速 + 买卖比 + 回撤保护
   → 从存活币中找起飞信号

5. 精筛后防线 (~数秒)
   BSCScan Top Holder 集中度 + 开发者行为分析 + GeckoTerminal 假K线检测
   → 排除庄家控盘、跑路币和控盘刷量币

6. 仿盘检测
   本地统计同名代币数量 (零 API 调用)
```

## 数据源

| 数据源 | 用途 | 限流 |
|--------|------|------|
| BSC RPC (publicnode) | 链上 TokenCreated 事件发现 | 无硬限制 |
| four.meme Detail API | 社交链接/持币数(bonding curve阶段)/进度/募资额 | ~5 req/s |
| DexScreener API | 批量价格+流动性+交易量+买卖笔数 | ~300 req/min |
| GeckoTerminal Token Info | 持币地址数 (已毕业代币, 链上索引) | ~30 req/min |
| BSCScan API (Etherscan V2) | Top Holder 集中度 + 开发者行为分析 (仅精筛后防线) | ~5 req/s |
| 本地队列统计 | 仿盘检测：同名/近似名代币数量 | 无 |

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
| 1 | 价格从峰值跌 90%+ | 暴跌弃盘 |
| 2 | 持币地址从 ≥30 跌破 10 | 大量抛售 |
| 3 | 持币数从峰值跌 70%+ (峰值≥50) | 僵尸币清理 |
| 4 | 无社交媒体 | 无运营意愿 |
| 5 | 流动性从 >$1k 跌破 $100 (仅已毕业) | 流动性枯竭 |
| 6 | 进度 < 1% 且币龄 > 2h | bonding curve 上的死币 |
| 6b | 进度 < 5% 且币龄 > 4h | 进度停滞 |
| 7 | 进度从峰值跌 50%+ 且币龄 > 6h | 热度消退 |
| 8 | 币龄 > 15min 且最高持币数 < 3 | 无人问津 |
| 9 | 币龄 > 1h 且最高持币数 < 5 | 热度不足 |
| 10 | 币龄 > 48h | 超出关注窗口 |

## 精筛规则（动量筛选）

从队列存活币中找"正在起飞"的信号，全部条件满足才通过：

| 条件 | 阈值 | 说明 |
|------|------|------|
| 币龄 | ≥ 15 分钟 | 数据稳定后再判断 |
| 持币地址数 | ≥ 15 | 最低持币门槛 |
| 价格动量 | 当前价 ≥ 入队价×1.5 或 ≥ 历史最低价×2.5 | 二选一，有资金进入信号 |
| 回撤保护 | 当前价 ≥ 峰值×0.5 | 从峰值跌超50%不推 |
| 持币增长 | 当前持币 ≥ 入队持币×1.5 或 近3轮持续递增 | 二选一，持续有人买入 |
| 进度 | ≥ 20% 且 < 97%，从 10%+ 跌破 5% 排除 | 低进度表现差；接近毕业/已毕业买入即亏 |
| 流动性 | ≥ $500（已毕业代币） | 排除流动性枯竭的僵尸币 |
| 买卖比 | ≥ 1.2 (买入笔数/卖出笔数) | 买压 > 卖压，有人在吸筹 |
| 价格加速度 | ≥ 5% (最近2轮价格变化率) | 价格正在加速上涨 |
| 持币增速 | ≥ 10% (最近2轮持币变化率) | 持币数正在加速增长 |
| 精筛冷却 | 同一代币通过后6轮内不再推送 | 减少重复信号噪音 |
| 仿盘数 | 仅标记, 不排除 | 仿盘多=热门信号, 🔥 标签展示, 交给用户判断 |

## 精筛后防线（深度检查）

仅对精筛通过的少量代币（通常个位数）执行，不影响整体扫描速度：

| 检查项 | 阈值 | 说明 |
|--------|------|------|
| Top10 持仓集中度 | ≤ 85% | BSCScan Top Holders，排除庄家控盘 |
| 开发者清仓 | 卖出 ≥ 90% | BSCScan Transfer 分析，开发者跑路信号 |
| 开发者撤池子 | LP token burn | 撤流动性，准备跑路 |
| 假K线检测 | 无影线实体柱 ≥ 80% 或全阳线 ≥ 90% | GeckoTerminal 15min K线，排除控盘刷量币 |

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
| `QUALITY_MIN_AGE_MIN` | 15 | 精筛: 币龄下限（分钟） |
| `QUALITY_MIN_HOLDERS` | 15 | 精筛: 持币地址数下限 |
| `QUALITY_PRICE_MOMENTUM_VS_ADDED` | 1.5 | 精筛: 当前价/入队价倍数 |
| `QUALITY_PRICE_MOMENTUM_VS_LOW` | 2.5 | 精筛: 当前价/历史最低价倍数 |
| `QUALITY_HOLDERS_GROWTH_VS_ADDED` | 1.5 | 精筛: 当前持币/入队持币倍数 |
| `QUALITY_HOLDERS_CONSEC_ROUNDS` | 3 | 精筛: 持币数连续递增轮数 |
| `QUALITY_MIN_PROGRESS` | 0.20 | 精筛: 进度下限 (20%) |
| `QUALITY_MIN_BUY_SELL_RATIO` | 1.2 | 精筛: 买卖比下限 |
| `QUALITY_MIN_PRICE_ACCEL` | 0.05 | 精筛: 价格加速度下限 (5%) |
| `QUALITY_MIN_HOLDERS_GROWTH_RATE` | 0.10 | 精筛: 持币增速下限 (10%) |
| `QUALITY_MAX_DRAWDOWN` | 0.50 | 精筛: 回撤保护 (当前价≥峰值×0.5) |
| `QUALITY_COOLDOWN_ROUNDS` | 6 | 精筛: 同一代币冷却轮数 |
| `QUALITY_MAX_TOP10_CONCENTRATION` | 0.85 | 精筛后防线: Top10持仓占比上限 |
| `QUALITY_FAKE_CANDLE_RATIO` | 0.80 | 精筛后防线: 无影线实体柱占比上限 |
| `QUALITY_FAKE_CANDLE_MIN_COUNT` | 4 | 精筛后防线: 假K线检测最少K线数 |
| `ELIM_PRICE_DROP_PCT` | 0.90 | 价格跌幅淘汰阈值 |
| `ELIM_HOLDERS_FLOOR` | 10 | 持币数淘汰下限 |
| `ELIM_LIQ_FLOOR` | 100 | 流动性淘汰下限（USD） |
| `ELIM_EARLY_PEAK_HOLDERS` | 3 | 币龄>15min 最高持币数淘汰下限 |
| `ELIM_MID_PEAK_HOLDERS` | 5 | 币龄>1h 最高持币数淘汰下限 |

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
- 核心函数：`discover_on_chain`、`admission_filter`、`elimination_check`、`quality_filter`、`post_quality_defense`
- 数据源函数：`ds_batch_prices`、`graduated_holder_counts` 等
- 文件头注释中的条件描述

## License

ISC
