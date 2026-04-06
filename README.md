# BSC Token Scanner

BSC 链上新代币扫描器，自动扫描最近 3 天内通过 [Four.meme](https://four.meme) 创建的代币，三级管线智能筛选展示。

## 数据源

| 数据源 | 用途 | 限流 |
|--------|------|------|
| four.meme Search API | 代币发现（批量列表） | ~2 req/s |
| four.meme Detail API | 代币详情（持币/社交链接/描述） | ~2 req/s |
| DexScreener API | K线价格数据（主要，快速） | ~300 req/min |
| GeckoTerminal OHLCV | K线数据（备选，精确） | ~30 req/min，自动退避重试 |
| 微博/Google/Twitter | 实时热点关键词（加分项） | 各平台独立限流 |

## 三级筛选管线

| 阶段 | 条件 | 数据源 | 请求开销 |
|------|------|--------|----------|
| 初筛 | 币龄≤3天、当前价≤$0.00002、持币地址粗筛 | Search API（批量） | 0 额外请求 |
| 详情筛 | 社交媒体≥1、持币地址(>1h:≥60,≤1h:≥30)、总量=10亿、当前价(≤1h:≤$0.000004,>1h:≤$0.00002) | Detail API | 每候选 1 请求 |
| K线筛 | 历史最高价≤$0.00004、前2h最高价≤$0.00002(币龄>1h)、当前价在最高价40%~90%、现价比底价高10%~50%(币龄>1h,排除首根K线) | DexScreener (主) + GeckoTerminal (备) | 每候选 1~3 请求 |

逐级收窄，避免不必要的 API 调用。

## 筛选规则

1. **社交媒体 ≥ 1**：至少关联 1 个社交媒体（Twitter/Telegram/Website）
   - 币龄 > 1 小时：持币地址数 ≥ 60
   - 币龄 ≤ 1 小时：持币地址数 ≥ 30
2. **价格条件**：总量 10 亿，历史最高价 ≤ 0.00004
   - 币龄 ≤ 1 小时：当前价 ≤ 0.000004
   - 币龄 > 1 小时：当前价 ≤ 0.00002
   - 币龄 > 1 小时：前 2 小时最高价 ≤ 0.00002（通过 GeckoTerminal K线精确计算）
3. **价格区间**：当前价在历史最高价的 40%~90% 之间
4. **底部区间**（币龄 > 1h）：当前价比除第一根 K 线外的所有 1h K 线最低价高 10%~50%
5. **热点新闻**（加分项，不作为筛选条件）：实时抓取社交媒体热点关键词，与代币名称/描述交叉匹配
   - 数据源：微博热搜 Top50、Google Trends（US/CN）、Twitter/X Trending
   - 匹配逻辑：短关键词(≤3字符)精确匹配名称，长关键词子串匹配，支持反向匹配
   - 按热点排名和来源加权评分，匹配到的代币标注 🔥

## 工作原理

1. GitHub Actions 定时任务（每 15 分钟）触发 `scripts/scan.js`
2. 通过 Four.meme Search API 获取代币列表（Stage 1 初筛）
3. 通过 Four.meme Detail API 获取详细信息：持币数、社交链接等（Stage 2 详情筛）
4. 通过 GeckoTerminal OHLCV API 获取 K线数据，计算真实历史最高价、前2h最高价和底价（Stage 3 K线筛）
5. `scripts/build.js` 将扫描数据整理到 `site/data/`，生成前端所需的静态文件
6. GitHub Pages 自动部署 `site/` 目录

## 项目结构

```
├── .github/workflows/
│   └── scan.yml              # GitHub Actions 定时任务（每 15 分钟）
├── data/                     # 扫描结果存档（按时间戳命名的 JSON）
├── scripts/
│   ├── scan.js               # 三级管线扫描 + 筛选 (four.meme + GeckoTerminal)
│   └── build.js              # 构建静态站点数据
├── public/
│   └── index.html            # 前端页面源文件
├── site/                     # 构建产物（部署到 GitHub Pages）
│   ├── index.html
│   └── data/
│       ├── latest.json       # 最新扫描结果
│       ├── history.json      # 历史扫描索引
│       └── scans/            # 各次扫描详情
└── package.json
```

## 本地开发

```bash
# 运行扫描（结果写入 data/）
node scripts/scan.js

# 构建静态站点（输出到 site/）
node scripts/build.js

# 本地预览
npx serve site
```

## GitHub 部署

1. 将代码推送到 GitHub 仓库
2. 在仓库 Settings → Pages 中，Source 选择 "GitHub Actions"
3. 工作流会自动每 15 分钟运行一次扫描并部署
4. 也可在 Actions 页面手动触发 `workflow_dispatch`

## 配置参数

以下常量定义在 `scripts/scan.js` 顶部：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_AGE_HOURS` | 72 | 扫描时间窗口（小时） |
| `TOTAL_SUPPLY` | 1,000,000,000 | 代币总量要求（10亿） |
| `MAX_CURRENT_PRICE_OLD` | 0.00002 | 币龄>1h 当前价格上限 |
| `MAX_CURRENT_PRICE_YOUNG` | 0.000004 | 币龄≤1h 当前价格上限 |
| `MAX_HIGH_PRICE` | 0.00004 | 历史最高价上限 (GeckoTerminal K线) |
| `MAX_EARLY_HIGH_PRICE` | 0.00002 | 币龄>1h时前2h最高价上限 (K线) |
| `PRICE_RATIO_LOW` | 0.4 | 当前价/最高价 下限 (40%) |
| `PRICE_RATIO_HIGH` | 0.9 | 当前价/最高价 上限 (90%) |
| `HOLDERS_THRESHOLD_OLD` | 60 | 币龄>1h 持币地址数阈值 |
| `HOLDERS_THRESHOLD_YOUNG` | 30 | 币龄≤1h 持币地址数阈值 |
| `MIN_SOCIAL_COUNT` | 1 | 最少社交媒体关联数 |

## License

ISC
