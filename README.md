# BSC Token Scanner

BSC 链上新代币扫描器，自动扫描最近 3 天内通过 [Four.meme](https://four.meme) TokenManagerV2 合约创建的代币，按持有人数筛选展示。

## 工作原理

1. GitHub Actions 定时任务（每 15 分钟）触发 `scripts/scan.js`
2. 扫描 BSC 链上 TokenManagerV2 合约的 TokenCreate 事件
3. 通过 Four.meme API 获取每个代币的持有人数
4. 筛选持有人数 ≥ 90 的代币，结果写入 `data/` 目录（JSON）
5. `scripts/build.js` 将扫描数据整理到 `site/data/`，生成前端所需的静态文件
6. GitHub Pages 自动部署 `site/` 目录

## 项目结构

```
├── .github/workflows/
│   └── scan.yml              # GitHub Actions 定时任务（每 15 分钟）
├── data/                     # 扫描结果存档（按时间戳命名的 JSON）
├── scripts/
│   ├── scan.js               # 链上扫描 + 持有人数查询
│   └── build.js              # 构建静态站点数据
├── public/
│   └── index.html            # 前端页面源文件
├── site/                     # 构建产物（部署到 GitHub Pages）
│   ├── index.html
│   └── data/
│       ├── latest.json       # 最新扫描结果
│       ├── history.json      # 历史扫描索引
│       ├── status.json       # 状态（静态站点始终 idle）
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
| `MIN_HOLDERS` | 90 | 持有人数筛选阈值 |
| `SCAN_RPC_URLS` | 3 个公共节点 | BSC RPC 端点，自动故障切换 |

## License

ISC
