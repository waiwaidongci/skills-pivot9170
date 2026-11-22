from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.constants.enums import Archive
from src.utils.time import now

class Dataset(Base):
    __tablename__ = 'datasets'
    __table_args__ = (UniqueConstraint('name', 'checksum'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(160))
    checksum: Mapped[str] = mapped_column(String(64))
    rows: Mapped[list | None] = mapped_column(JSON)
    row_count: Mapped[int]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    # 归档：rows 清除后，archive_rows 保存月 × 渠道 × 项目的人数聚合；NULL 表示未发起归档。
    archive_status: Mapped[str | None] = mapped_column(String(20), default=None, index=True)
    archive_rows: Mapped[list | None] = mapped_column(JSON)
    archive_note: Mapped[str | None] = mapped_column(String(500))
    archived_at: Mapped[datetime | None]


class ArchiveJob(Base):
    """批次归档任务，由 worker 驱动，可安全重试且不会重复切换。"""
    __tablename__ = 'archive_jobs'

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), unique=True)
    status: Mapped[str] = mapped_column(String(20), default=Archive.PENDING, index=True)
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(String(500))
    switched: Mapped[bool] = mapped_column(default=False)
    purged: Mapped[bool] = mapped_column(default=False)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime | None]


class PurgedChecksum(Base):
    """已进入归档控制的内容指纹：同字节明细即使改名重传也不得找回逐笔记录。"""
    __tablename__ = 'purged_checksums'

    checksum: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    created_at: Mapped[datetime] = mapped_column(default=now)
