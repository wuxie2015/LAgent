## Context
用户已在聊天中审阅方案并要求执行。当前为单用户 Python CLI，记忆 JSON 由主聊天独占；后台不能共享 Agent/MemoryManager。沿用现有分层结构、unittest 和工具协议。

## Goals / Non-Goals
交付持久化调度、管理、隔离任务执行、通知、重启恢复与可观察记录。第一版时间规则为 once/daily/weekly（可选择多个周几）；不提供服务器部署、cron 表达式或关机唤醒。

## Decisions
- `src/domain/schedule.py`：时区及时间规则的纯函数。接受 IANA 时区，存储 UTC 时间；本地重复规则保持当地墙钟时间。拒绝 DST 不存在/有歧义时间，工具要求明确偏移解决。
- `src/service/schedule_store.py`：SQLite tasks/runs，连接按操作创建，事务原子领取，唯一 task_id/scheduled_for。数据库默认在忽略的 data/ 下。调度 worker 另持有 OS 文件锁，防止多个活跃执行器；重启不重放执行中的 Agent 任务，记录 unknown。已生成结果持久化后可独立重试通知。
- 状态：task active/paused/cancelled/completed；run executing/pending_notification/delivered/failed/missed/unknown。claim_due 每次领取一个到期任务，并推进其下次时间。超过 grace_seconds 的任务记 missed，重复任务跳到未来，不批量补发。暂停/取消阻止未开始的任务及待重试通知，运行中任务不能强制撤销已发生副作用。
- `src/service/notifications.py`：console、SMTP 和 Webhook 适配器。地址/密钥仅配置，schedule 工具只选择渠道名。接口 send(channel, subject, body, *, task_id, run_id)。HTTP/SMTP 有超时，日志和数据库不保存凭据或原始网络错误。
- `src/application/scheduler.py`：后台线程轮询、可停止，单 worker 串行。reminder 直接形成正文；agent 通过注入执行函数，在新建 Agent/无持久记忆下执行。后台默认仅 web_search，不提供 shell、schedule 工具；外部操作权限不因定时而增加。任务异常不自动重跑，通知失败有限次退避。关闭时先停止并等待 worker，再释放存储资源。
- `src/tools/schedule.py`：结构化 create/list/update/pause/resume/cancel/history 操作。明确输出绝对时间、时区、渠道和重复规则，缺配置拒绝创建。创建/更新必须绑定唯一 user request key（重复工具调用不重复创建），提醒文本不经过大模型生成。
- CLI 随启动接入后台调度（可关闭），提供 /schedule list/history/pause/resume/cancel；系统提示解释自然语言时间转换、真实当前时间、默认渠道和工具回执。现有记忆 /task 与定时任务区分，清除聊天不取消定时任务。
- 配置复用 Settings，标准库实现，无新依赖。规划/验收主控，Codex gpt-6-luna 子代理执行；这是当前暴露的最近 Luna 版本。仅暂存，不提交或推送。

## Risks / Trade-offs
程序退出和主机休眠会延迟 → grace 策略和文档说明。外发成功但状态写盘前崩溃 → 通知可能重复，Webhook 提供 run_id 供接收端去重，不承诺 exactly-once。SMTP/HTTP 实际可达性须用户配置后验证，本次测试使用本地替身，不发送真实通知。

## Migration Plan
新增独立数据库，无现有记忆迁移；禁用 scheduler 即停止执行，保留数据库。对启动/退出及原有 CLI 作回归测试。
