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
   DexScreener 批量查价 + Detail API 查持币数 → 永久淘汰弃盘币

4. 精筛 (~10s)
   K线/价格比/底价区间 → 输出推荐
```

## 数据源

| 数据源 | 用途 | 限流 |
|--------|------|------|
| BSC RPC (publicnode) | 链上 TokenCreated 事件发现 | 无硬限制 |
| four.meme Detail API | 社交链接/进度 | ~5 req/s |
| BscScan API (Etherscan V2) | 链上真实持币地址数 | ~5 req/s |
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
| 5 | 进度 < 1% 且币龄 > 4h | bonding curve 上的死币 |
| 6 | 币龄 > 5min 且最高持币数 < 3 | 无人问津 |
| 7 | 币龄 > 15min 且最高持币数 < 5 | 无人问津 |
| 8 | 币龄 > 1h 且最高持币数 < 10 | 热度不足 |
| 9 | 币龄 > 72h | 超出关注窗口 |

## 精筛规则

对队列中存活代币执行：

- 持币地址数：币龄 >1h ≥ 60，≤1h ≥ 30
- 当前价：≤1h ≤ $0.000004，>1h ≤ $0.00002（币龄<4h 且价>$0.00001 时放宽）
- 历史最高价 ≤ $0.00004
- 前 2h 最高价 ≤ $0.00002（币龄 >1h，K线计算）
- 当前价在最高价 40%~90%
- 现价比底价高 10%~100%
- 热点匹配（加分项）：微博热搜关键词与代币名称交叉匹配，标注 🔥

## 前端功能

三个 Tab 视图：

- **精筛结果**：通过全部筛选条件的推荐代币
- **队列存活**：当前队列中所有存活代币（含价格/持币/流动性/峰值等）
- **本轮淘汰**：本轮被淘汰的代币及淘汰原因

支持通过历史记录查看任意时刻的队列快照。

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
  "bscscanApiKey": "YOUR_BSCSCAN_API_KEY"
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

## License

ISC
