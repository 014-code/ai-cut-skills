---
name: aivideoeditor-soda-music-upload
description: "将已验收的汽水音乐视频按 UserGrowth 的 soda_music 流程上传、录入变色龙、送审并回收任务证据。用于汽水音乐分发上传，不用于番茄音乐打标、红果短剧或视频生成。"
metadata:
  short-description: 汽水音乐 UserGrowth 上传与送审
---

# 汽水音乐自动化上传

这个 Skill 是汽水音乐分发流程的独立入口。它只处理 `workflow=soda_music`：选择已经生成并验收的视频，执行 UserGrowth 上传、录入变色龙、送审、CID 回收和任务产物记录。

## 输入契约

优先使用平台已经入库的源任务和产物，避免把本地路径直接交给线上服务：

- `workflow`：固定为 `soda_music`。
- `sourceJobIds`：已经完成的手动入库或裂变任务 ID 列表。
- `filters.artifactIds`：可选，限定本次上传的产物。
- `target.orderId`：UserGrowth 上传订单号。
- `target.playletBid`：汽水音乐业务 BID，使用数字 BID 或 `bid_<数字>`。
- `target.live` 与 `target.confirmLive`：正式上传必须同时为 `true`；预检或演练保持 `false`。

线上调用只传非敏感的账号引用（如 `usergrowthAccountRef`）。UserGrowth 账号、浏览器会话和密码由平台的账号配置管理，禁止放入 Skill 入参、日志、任务清单或 MCP 审计记录。

## 固定流程

1. 校验输入任务属于当前用户，且所有源任务已完成。
2. 按 `artifactIds` 或用户明确的选择建立单一 Soda 上传计划，不混入前贴/尾贴任务。
3. 通过 UserGrowth 浏览器完成上传、录入变色龙和送审。
4. 回收每个视频的 CID、任务 ID、状态和失败原因，写入 `task.json`、`run.log` 等受控产物。
5. 只有所有请求项都有明确结果时才报告完成；部分成功必须保留失败项和可恢复任务信息。

## 边界

- 这是汽水音乐上传 Skill，不负责生成视频、正文混剪、番茄音乐 CID/BID 打标或红果短剧 ARLP。
- 正式上传、送审或修改线上数据前必须得到用户明确确认；默认先做 dry-run/计划校验。
- 不得改变现有上传 -> 变色龙 -> 送审 -> CID 回收的阶段顺序、断点语义和成功判定。
- 本包用于线上 Skill 目录和 MCP 能力契约展示；入口脚本会复用已验证的 UserGrowth 自动化实现及平台账号配置，不复制浏览器私有实现。

## 现有实现

本地运行时仍使用综合 UserGrowth Skill 的正式入口：

```powershell
python "$env:USERPROFILE\.codex\skills\aivideoeditor-usergrowth-automation\scripts\usergrowth_upload.py" --help
```

需要执行真实上传时，遵循该 Skill 的 `references/standalone-cli.md`、`references/workflow.md` 和 `references/browser-flow.md`，并使用 `--live --confirm-live`。
