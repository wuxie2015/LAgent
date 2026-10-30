## Why
用户需要应用启动后主动执行提醒和预定任务，并通过配置渠道接收结果。现有记忆中的任务只是状态记录，不具备时钟调度和通知能力。

## What Changes
- 持久化一次性、每天和每周的定时提醒及 Agent 任务，支持查询、修改、暂停、恢复和取消。
- 应用启动后台调度器，关闭时停止；支持错过时间处理、通知重试和执行记录。
- 新增 console、SMTP 邮件和配置式 Webhook 通知渠道；凭据仅来自环境配置。
- 自然语言通过 schedule 工具操作，并提供 /schedule 命令检查状态。

## Capabilities
### New Capabilities
- `scheduled-outreach`: 持久化调度、隔离执行、通知和任务管理。
### Modified Capabilities
无。

## Impact
Python 标准库 SQLite、线程、SMTP、HTTP，不增加第三方依赖。新增调度领域、存储、通知、后台工作线程和工具，修改 CLI、Settings、系统提示词、示例配置及说明；保留现有分层记忆语义。应用退出或主机休眠期间不保证准时触发。
