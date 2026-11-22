# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重。`rows` 在归档清除后为 NULL，人数聚合保留在 `archive_rows`。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。
- `schedules` 保存定时计划和下一到期时间。
- `deliveries` 保存站内分发回执，每次运行最多一个回执。
- `audit_events` 记录登录、数据入库、运行、复核、报表分发和归档动作。

# 002 受理批归档

- `datasets` 新增可空列 `archive_status`、`archive_rows`、`archive_note`、`archived_at`；`rows` 允许为 NULL。未发起归档时 `archive_status` 为 NULL。
- `archive_jobs` 记录归档任务的租约、阶段标记（`switched` / `purged`）与重试次数，`dataset_id` 唯一。
- `purged_checksums` 以内容指纹为主键：进入归档控制的明细即使改名并发重传也被拒绝，无法找回已清除的逐笔记录。
- 新建库由 `Base.metadata.create_all` 直接建出全部表；升级既有库需按本文件手动补列补表，并把 `datasets.rows` 改为可空。
