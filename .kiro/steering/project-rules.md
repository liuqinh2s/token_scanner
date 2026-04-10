---
inclusion: always
---

# 项目规则

## 一致性同步

每次修改功能逻辑、筛选条件、数据结构或架构设计时，必须同步更新以下所有相关位置：

1. **`scripts/scan.js`** — 文件头注释、常量定义、代码逻辑
2. **`scripts/build.js`** — 文件头注释、数据处理逻辑
3. **`public/index.html`** — subtitle、filter-desc 文案、表格列、渲染逻辑
4. **`README.md`** — 架构说明、数据源表、规则表、配置参数表、项目结构
5. **`site/index.html`** — 通过 `npm run build` 重新生成

不允许只改代码不改文档，也不允许只改文档不改代码。改完后跑一次 `npm run scan && npm run build` 验证全链路。

## 项目语言

- 代码注释、日志输出、前端文案统一使用中文
- 变量名、函数名使用英文
- README 使用中文

## 架构要点

- 代币发现来源是 BSC 链上 RPC `eth_getLogs`，不是 four.meme Search API
- 队列状态持久化在 `data/queue.json`，扫描结果按时间戳存在 `data/` 目录
- 每轮扫描结果包含 `tokens`（精筛通过）、`queue`（存活快照）、`eliminatedThisRound`（本轮淘汰）三个数组
- `build.js` 处理 `data/` 时必须排除 `queue.json`
- 前端有三个 Tab：精筛结果、队列存活、本轮淘汰
