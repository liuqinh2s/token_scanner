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
   DexScreener 批量查价 + RPC Transfer事件/BscScan 查持币数 + Detail API → 永久淘汰弃盘币

4. 钱包行为分析 (~20s)
   BscScan tokentx → 开发者行为 (DEX Router/LP token mint-burn)
   Top Holders 交叉分析 → 聪明钱自动发现 (1h缓存)
   聪明钱行为追踪 → 排除/加分信号

5. 精筛 (~10s)
   K线/价格比/底价区间 + 钱包行为排除/加分 → 输出推荐
```

## 数据源

| 数据源 | 用途 | 限流 |
|--------|------|------|
| BSC RPC (publicnode) | 链上 TokenCreated 事件发现 | 无硬限制 |
| four.meme Detail API | 社交链接/进度 | ~5 req/s |
| BscScan API (Etherscan V2) | tokentx 开发者行为 + Top Holders 聪明钱自动发现 | ~5 req/s |
| BSC RPC Transfer 事件 | 持币地址数 (免费, 替代 BscScan PRO 端点) | 无硬限制 |
| DexScreener API | 批量价格+流动性查询 | ~300 req/min |
| GeckoTerminal OHLCV | K线数据（精筛用） | ~30 req/min |
| 微博热搜 | 热点关键词匹配（加分项） | 独立限流 |

## 淘汰规则（永久剔除）

满足任一条件即从队列中永久移除，不再关注：

| # | 条件 | 说明 |
|---|------|------|
| 1 | 价格从峰值跌 90%+ | 暴跌弃盘 |
| 2 | 持币地址从 30+ 跌破 10 | 大量抛售 |
| 3 | 无社交媒体 | 无运营意愿 |
| 4 | 流动性从 >$1k 跌破 $100 | 流动性枯竭 |
| 5 | 进度 < 1% 且币龄 > 2h | bonding curve 上的死币 |
| 5b | 进度 < 5% 且币龄 > 4h | bonding curve 进展缓慢 |
| 6 | 币龄 > 5min 且最高持币数 < 3 | 无人问津 |
| 7 | 币龄 > 15min 且最高持币数 < 5 | 无人问津 |
| 8 | 币龄 > 1h 且最高持币数 < 10 | 热度不足 |
| 9 | 币龄 > 72h | 超出关注窗口 |

## 钱包行为分析

通过 BscScan API 追踪开发者和聪明钱的链上行为（参考 [token_trading](https://github.com/liuqinh2s/token_trading) 方案）。

### 开发者行为判定

- 开发者地址来源：链上 `TokenCreated` 事件解码的 `creator` 字段
- 转账查询：BscScan `tokentx` 查开发者对该代币的转账记录
- 买入判定：从 DEX Router（PancakeSwap V2/V3/Universal）或零地址收到代币
- 卖出判定：转到 DEX Router 或其他非自己地址（转到零地址/死地址视为销毁，不算卖出）
- 清仓判定：卖出占收到总量 ≥ 90%
- 流动性操作：通过 LP token 的 mint/burn 检测（from=0x0 → 加池子，to=0x0 → 撤池子）

### 聪明钱自动发现

聪明钱地址无需手动维护，支持自动发现（1小时刷新）：

1. 手动配置：`config.local.json` 的 `smartMoneyAddrs` 数组
2. Top Holders 交叉分析：GeckoTerminal BSC trending pools 筛选24h涨幅>10%的已上DEX代币 → RPC Transfer 事件统计每个代币 Top 50 Holders → 在 ≥2 个代币中都是大户的地址自动识别为聪明钱
3. 排除已知非聪明钱地址：交易所合约、DEX Router、WBNB、USDT、BUSD、USDC 等

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
  "smartMoneyAddrs": ["0x...", "0x..."],
  "smartMoneyCrossFreq": 2
}
```

- `smartMoneyAddrs`：手动补充的聪明钱地址（可选，自动发现已覆盖大部分）
- `smartMoneyCrossFreq`：Top Holders 交叉分析最小出现频次（默认 2）

## 精筛规则

对队列中存活代币执行：

- 持币地址数：币龄 >1h ≥ 60，≤1h ≥ 30
- 当前价：≤1h ≤ $0.000004，>1h ≤ $0.00002（币龄<4h 且价>$0.00001 时放宽）
- 历史最高价 ≤ $0.00004
- 前 2h 最高价 ≤ $0.00002（币龄 >1h，K线计算）
- 当前价在最高价 40%~90%
- 现价比底价高 10%~100%
- 热点匹配（加分项）：微博热搜关键词与代币名称交叉匹配，标注 🔥
- 钱包行为（排除/加分）：开发者/聪明钱减仓清仓撤池子排除，加仓加池子加分标注

## 前端功能

三个 Tab 视图：

- **精筛结果**：通过全部筛选条件的推荐代币
- **队列存活**：当前队列中所有存活代币（含价格/持币/流动性/峰值等）
- **本轮淘汰**：本轮被淘汰的代币及淘汰原因
- **入场淘汰**：新发现但未通过入场筛的代币及原因（无社交/总量不符等）

搜索功能覆盖全部四个 Tab 的历史数据，任何代币都可以搜到并查看淘汰原因。

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

`npm run scan` 即 `node scripts/scan.js`，执行一次完整扫描流程：链上发现新代币 → 入场筛 → 淘汰检查 → 精筛输出。扫描结果保存在 `data/` 目录，队列状态持久化在 `data/queue.json`。GitHub Actions 每 15 分钟自动执行一次。

## 配置

`config.local.json`（已 gitignore）：

```json
{
  "proxy": { "enabled": true, "host": "127.0.0.1", "port": 7890 },
  "bscscanApiKey": "YOUR_BSCSCAN_API_KEY",
  "smartMoneyAddrs": ["0x...", "0x..."],
  "smartMoneyCrossFreq": 2
}
```

`scripts/scan.js` 顶部常量：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_AGE_HOURS` | 72 | 关注窗口（小时） |
| `SCAN_INTERVAL_MIN` | 15 | 扫描间隔（分钟） |
| `ELIM_PRICE_DROP_PCT` | 0.90 | 价格跌幅淘汰阈值 |
| `ELIM_HOLDERS_FLOOR` | 10 | 持币数淘汰下限 |
| `ELIM_LIQ_FLOOR` | 100 | 流动性淘汰下限（USD） |
| `ELIM_EARLY_PEAK_HOLDERS` | 5 | 币龄>15min 最高持币数淘汰下限 |
| `ELIM_TINY_PEAK_HOLDERS` | 3 | 币龄>5min 最高持币数淘汰下限 |
| `ELIM_MID_PEAK_HOLDERS` | 10 | 币龄>1h 最高持币数淘汰下限 |
| `SMART_MONEY_MIN_CROSS_FREQ` | 2 | Top Holders 交叉分析最小出现频次 |
| `SMART_MONEY_CACHE_TTL` | 3600000 | 聪明钱地址缓存时间（毫秒，1小时） |

## License

ISC
