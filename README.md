# BSC Token Scanner

BSC 链上新代币扫描器。直接扫描链上 [Four.meme](https://four.meme) 合约的 `TokenCreated` 事件发现新代币，采用**队列淘汰制**持续跟踪，自动剔除弃盘币，对存活代币执行精筛。

## v2 架构：链上发现 + 队列淘汰制

旧版通过 four.meme Search API 拉取代币列表，但实测该 API 只能覆盖平台约 1/3 的代币。v2 改为直接扫链上事件，100% 覆盖。

```
每 15 分钟执行一次:

1. 链上发现 (~1s)
   BSC RPC eth_getLogs → four.meme TokenCreated 事件 → 新代币地址
   
2. 入场筛 (~35s)
   four.meme Detail API → 淘汰无社交 / 总量≠10亿

3. 淘汰检查 (~15s)
   DexScreener 批量查价 + GeckoTerminal 持币数 + Detail API → 永久淘汰弃盘币

4. 钱包行为分析 (~20s)
   BscScan tokentx → 开发者行为 (DEX Router/LP token mint-burn)
   GMGN API → 聪明钱地址获取 (持久化到文件)
   聪明钱行为追踪 → 排除/加分信号

5. 精筛 (~10s)
   K线/价格比/底价区间 + 钱包行为排除/加分 → 输出推荐
```

## 数据源

| 数据源 | 用途 | 限流 |
|--------|------|------|
| BSC RPC (publicnode) | 链上 TokenCreated 事件发现 | 无硬限制 |
| four.meme Detail API | 社交链接/持币数(bonding curve阶段)/进度 | ~5 req/s |
| DexScreener API | 批量价格+流动性查询 | ~300 req/min |
| GeckoTerminal Token Info | 持币地址数 (已毕业代币, 链上索引) | ~30 req/min |
| 本地队列统计 | 仿盘检测：同名/近似名代币数量（零 API 调用） | 无 |

### 持币数查询方案

持币数是筛选和淘汰的核心指标，但 four.meme 代币的生命周期跨越 bonding curve 和 DEX 两个阶段，没有单一数据源能覆盖全程。当前采用多源互补策略：

**优先级: GeckoTerminal > BSCScan 网页爬取 > four.meme Detail API > 缓存**

| 数据源 | 覆盖阶段 | 说明 |
|--------|----------|------|
| GeckoTerminal `/tokens/{addr}/info` | 已毕业 (DEX 阶段) | 免费、无需 key，返回 `holders.count`，链上索引数据，最准确 |
| BSCScan 网页爬取 | 已毕业 (DEX 阶段) | GT 查不到时的降级备选，爬取 `bscscan.com/token/{addr}` 页面 |
| four.meme Detail API `holderCount` | Bonding curve 阶段 | 平台内部记账，毕业后返回 0 |
| 队列缓存 | 兜底 | 上一轮的持币数，避免数据断档 |

注意：只对已毕业代币（progress ≥ 1）发起 GT/BSCScan 查询，未毕业代币直接用 detail API，避免浪费请求。

**已验证不可用的方案及原因：**

| 方案 | 问题 |
|------|------|
| BSCScan `tokenholdercount` API | 免费 API key 不支持 BSC 链 (`NOTOK: Free API access is not supported for this chain`) |
| RPC `eth_getLogs` Transfer 事件 | Bonding curve 阶段的买卖通过 four.meme 合约内部记账，不产生标准 ERC-20 Transfer 事件。实测某代币 385 个持币者中仅 ~50 个出现在 Transfer 日志中 |
| DexScreener API | 不返回持币地址数字段 |
| CoinGecko 免费 API | 小代币未收录 (404) |
| BSCTrace API | 域名无法连接 |

**备选方案（未实现）：**

- CoinGecko 付费 API：Token Info 端点支持 holders，但需要 API key

## 淘汰规则（永久剔除）

满足任一条件即从队列中永久移除，不再关注：

| # | 条件 | 说明 |
|---|------|------|
| 1 | 价格从峰值跌 90%+ | 暴跌弃盘 |
| 2 | 持币地址从 ≥30 跌破 10 | 大量抛售 |
| 3 | 无社交媒体 | 无运营意愿 |
| 4 | 流动性从 >$1k 跌破 $100 | 流动性枯竭（当前数据源无法提供有效流动性，实际不生效） |
| 5 | 进度 < 1% 且币龄 > 2h | bonding curve 上的死币 |
| 5b | 进度 < 5% 且币龄 > 4h | bonding curve 进展缓慢 |
| 7 | 币龄 > 15min 且最高持币数 < 3 | 无人问津 |
| 8 | 币龄 > 1h 且最高持币数 < 5 | 热度不足 |
| 8 | 币龄 > 48h | 超出关注窗口 |

### 开发者行为判定

- 开发者地址来源：链上 `TokenCreated` 事件解码的 `creator` 字段
- 转账查询：BscScan `tokentx` 查开发者对该代币的转账记录
- 买入判定：从 DEX Router（PancakeSwap V2/V3/Universal）或零地址收到代币
- 卖出判定：转到 DEX Router 或其他非自己地址（转到零地址/死地址视为销毁，不算卖出）
- 清仓判定：卖出占收到总量 ≥ 90%
- 流动性操作：通过 LP token 的 mint/burn 检测（from=0x0 → 加池子，to=0x0 → 撤池子）

### 聪明钱地址获取

聪明钱地址通过 GMGN API 获取，持久化到 `data/smart_money.json`，每轮扫描增量更新：

1. GMGN API：调用 `/v1/user/smartmoney?chain=bsc` 获取 BSC 聪明钱地址列表，合并新地址到本地文件
2. 排除已知非聪明钱地址：交易所合约、DEX Router、WBNB、USDT、BUSD、USDC 等
3. 匹配方式：在已有的 RPC Transfer 日志中匹配聪明钱地址的买入/卖出行为

聪明钱行为判定：
- 买入：从 DEX Router 或零地址收到代币，按地址数计数
- 卖出：转到 DEX Router，按地址数计数

### 精筛排除（有以下行为直接排除，不进精筛结果）

| 行为 | 判定条件 | 说明 |
|------|----------|------|
| 开发者减仓 | 有卖出交易（转到 DEX Router 或其他地址） | 开发者在抛售 |
| 开发者清仓 | 卖出占收到总量 ≥ 90% | 开发者大量抛售 |
| 开发者撤池子 | LP token burn（from=creator, to=0x0） | 撤流动性，准备跑路 |
| 聪明钱减仓 | 有卖出交易（转到 DEX Router） | 聪明钱在离场 |

### 精筛加分（标签显示在代币名称旁）

| 信号 | 判定条件 | 标签 |
|------|----------|------|
| 开发者加仓 | 从 DEX Router/零地址收到代币 | 💰 开发者加仓 |
| 开发者加池子 | LP token mint（from=0x0, to=creator） | 🏊 开发者加池子 |
| 聪明钱加仓 | 从 DEX Router/零地址收到代币（按地址数计分） | 🧠 聪明钱加仓 |

### 配置

在 `config.local.json` 中可选配置：

```json
{
  "gmgn_api_key": "YOUR_GMGN_API_KEY"
}
```

- `gmgn_api_key`：GMGN API Key，用于获取聪明钱地址列表

## 精筛规则

| 条件 | 阈值 | 说明 |
|------|------|------|
| 币龄 | ≥ 3 分钟 | 数据稳定后再判断 |
| 持币地址数 | ≥ 15 | 最低持币门槛 |
| 价格动量 | 当前价 ≥ 入队价×1.5 或 ≥ 历史最低价×2.5 | 二选一，有资金进入信号 |
| 持币增长 | 当前持币 ≥ 入队持币×1.5 或 近3轮持续递增 | 二选一，持续有人买入 |
| 进度 | ≥ 10%，且从 10%+ 跌破 5% 排除 | 衰退中的币排除 |
| 流动性 | ≥ $500（已毕业代币） | 排除流动性枯竭的僵尸币 |
| 回撤保护 | 当前价 ≥ 峰值×0.5 | 从峰值跌超50%不推 |
| 精筛冷却 | 同一代币通过后6轮内不再推送 | 减少重复信号噪音 |
| 仿盘数 | 仅标记, 不排除 | 仿盘多=热门信号, 🔥 标签展示, 交给用户判断 |

## 前端功能

四个 Tab 视图：

- **精筛结果**：通过全部筛选条件的推荐代币
- **队列存活**：当前队列中所有存活代币（含价格/持币/流动性/峰值等）
- **本轮淘汰**：本轮被淘汰的代币及淘汰原因
- **入场淘汰**：新发现但未通过入场筛的代币及原因（无社交/总量不符等）

搜索功能覆盖全部四个 Tab 的历史数据，任何代币都可以搜到并查看淘汰原因。用合约地址搜索时，会展示该代币在所有历史扫描时间点的快照（价格、持币数、流动性等变化），方便追踪代币的完整生命周期。

## 项目结构

```
├── .github/workflows/
│   └── scan.yml              # GitHub Actions 定时任务
├── data/
│   ├── queue.json            # 队列状态（代币列表 + 已淘汰记录 + lastBlock）
│   └── 2026-04-10T*.json     # 每轮扫描结果（含 queue/eliminated 快照）
├── scripts/
│   ├── scan.js               # v2 扫描：链上发现 + 队列淘汰 + 精筛
│   ├── build.js              # 构建静态站点数据
│   └── compare.js            # 链上 vs API 覆盖率比对工具
├── public/
│   └── index.html            # 前端页面源文件
├── site/                     # 构建产物（GitHub Pages）
│   ├── index.html
│   └── data/
│       ├── latest.json       # 最新扫描结果（含队列快照）
│       ├── history.json      # 历史扫描索引
│       └── scans/            # 各次扫描详情
└── package.json
```

## 使用方法

```bash
npm run scan                    # 执行一次扫币（链上发现 + 队列淘汰 + 精筛）
npm run build                   # 构建静态站点
npm run scan && npm run build   # 扫币 + 构建（完整流程）
npm run dev                     # 启动开发服务器（live-server）
```

`npm run scan` 即 `node scripts/scan.js`，执行一次完整扫描流程：链上发现新代币 → 入场筛 → 淘汰检查 → 精筛输出。扫描结果保存在 `data/` 目录（保留近 48 小时），队列状态持久化在 `data/queue.json`。GitHub Actions 每 15 分钟自动执行一次。

## 配置

`config.local.json`（已 gitignore）：

```json
{
  "proxy": { "enabled": true, "host": "127.0.0.1", "port": 7890 },
  "bscscanApiKey": "YOUR_BSCSCAN_API_KEY",
  "gmgn_api_key": "YOUR_GMGN_API_KEY"
}
```

`scripts/scan.py` 顶部常量：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_AGE_HOURS` | 48 | 关注窗口（小时） |
| `SCAN_INTERVAL_MIN` | 15 | 扫描间隔（分钟） |
| `QUALITY_MIN_AGE_MIN` | 3 | 精筛: 币龄下限（分钟） |
| `QUALITY_MIN_HOLDERS` | 15 | 精筛: 持币地址数下限 |
| `QUALITY_PRICE_MOMENTUM_VS_ADDED` | 1.5 | 精筛: 当前价/入队价倍数 |
| `QUALITY_PRICE_MOMENTUM_VS_LOW` | 2.5 | 精筛: 当前价/历史最低价倍数 |
| `QUALITY_HOLDERS_GROWTH_VS_ADDED` | 1.5 | 精筛: 当前持币/入队持币倍数 |
| `QUALITY_HOLDERS_CONSEC_ROUNDS` | 3 | 精筛: 持币数连续递增轮数 |
| `QUALITY_MIN_PROGRESS` | 0.10 | 精筛: 进度下限 (10%) |
| `QUALITY_PROGRESS_DROP_PEAK` | 0.10 | 精筛: 进度跌破判定峰值 (10%) |
| `QUALITY_PROGRESS_DROP_FLOOR` | 0.05 | 精筛: 进度跌破排除线 (5%) |
| `QUALITY_MIN_LIQUIDITY_GRAD` | 500 | 精筛: 已毕业代币流动性下限（USD） |
| `QUALITY_MAX_DRAWDOWN` | 0.50 | 精筛: 回撤保护 (当前价≥峰值×0.5) |
| `QUALITY_COOLDOWN_ROUNDS` | 6 | 精筛: 同一代币冷却轮数 |
| `ELIM_PRICE_DROP_PCT` | 0.90 | 价格跌幅淘汰阈值 |
| `ELIM_HOLDERS_FLOOR` | 10 | 持币数淘汰下限 |
| `ELIM_LIQ_FLOOR` | 100 | 流动性淘汰下限（USD） |
| `ELIM_EARLY_PEAK_HOLDERS` | 3 | 币龄>15min 最高持币数淘汰下限 |
| `ELIM_MID_PEAK_HOLDERS` | 5 | 币龄>1h 最高持币数淘汰下限 |
| `SMART_MONEY_FILE` | `data/smart_money.json` | 聪明钱地址持久化文件路径 |

## License

ISC
