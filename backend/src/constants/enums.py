from enum import StrEnum

class Role(StrEnum):
    ADMIN = 'admin'
    ANALYST = 'analyst'
    VIEWER = 'viewer'

class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'

class Review(StrEnum):
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    RETURNED = 'returned'

class Archive(StrEnum):
    PENDING = 'pending'          # 已发起，等待后台核对人数
    RECONCILING = 'reconciling'  # 人数核对中（租约内）
    SWITCHED = 'switched'        # 人数核对一致，新任务已改读归档人数
    PURGING = 'purging'          # 等待引用原明细的任务结束
    COMPLETE = 'complete'        # 原明细已清除，仅保留归档人数
    FAILED = 'failed'            # 核对失败或多次中断，原明细保持不变
