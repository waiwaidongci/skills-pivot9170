"""受理批次归档的后台流程。

阶段一（人数核对与切换）：worker 租约领取归档任务，用原批明细重建月度归属，
与归档聚合逐项核对；一致后在同一事务写入 archive_rows、置 switched 标记并把
批次切到「已切换」。中断重试用 switched 标记跳过，绝不重复切换。

阶段二（清除原明细）：所有引用该批次的运行结束后，才把 rows 置空。
仍有排队或计算中的任务时保持「等待清除」，每个 worker 周期无痛复查。
"""

import logging
from datetime import timedelta
from sqlalchemy import func, or_, select
from src.config.database import Session
from src.constants.enums import Archive, Status
from src.domain.archiving import build_archive, reconcile
from src.models.jobs import Run
from src.models.source import ArchiveJob, Dataset, PurgedChecksum
from src.services.audit import record
from src.utils.time import now

log = logging.getLogger(__name__)
_ACTIVE_RUN_STATES = (Status.QUEUED, Status.RUNNING)
_MAX_ATTEMPTS = 3


def start(db, dataset_id, actor):
    """发起归档：预校验明细并登记任务，真正的核对与清除由 worker 完成。"""
    dataset = db.get(Dataset, dataset_id, with_for_update=True)
    if dataset is None:
        raise LookupError('找不到该数据批次')
    if dataset.archive_rows is not None or dataset.archive_status is not None:
        raise ValueError('该批次已归档或已有进行中的归档任务，请勿重复发起。')
    existing = db.scalar(select(ArchiveJob).where(ArchiveJob.dataset_id == dataset_id))
    if existing is not None:
        raise ValueError('该批次已有归档任务，可在归档进度中查看。')
    # 发起前先做一次口径校验与聚合预演，明显的数据问题当场驳回。
    archived_rows, summary = build_archive(dataset.rows)
    job = ArchiveJob(dataset_id=dataset.id, status=Archive.PENDING, author=actor)
    db.add(job)
    db.flush()
    # 立即登记内容指纹：归档进行中改名并发重传同内容文件同样会被拒绝。
    if db.get(PurgedChecksum, dataset.checksum) is None:
        db.add(PurgedChecksum(checksum=dataset.checksum, dataset_id=dataset.id))
    dataset.archive_status = Archive.PENDING
    dataset.archive_note = '等待后台核对归档人数'
    record(db, actor, 'archive.started', dataset.id, job_id=job.id,
           months=summary['months'], people=summary['intake_people'])
    return job, summary, archived_rows


def claim():
    """领取一个待核对的归档任务，返回租约上下文；PURGING 任务不走租约。"""
    stamp = now()
    with Session.begin() as db:
        query = select(ArchiveJob).where(or_(
            ArchiveJob.status == Archive.PENDING,
            (ArchiveJob.status == Archive.RECONCILING) & (ArchiveJob.lease_until < stamp),
        )).order_by(ArchiveJob.id).limit(1).with_for_update(skip_locked=True)
        job = db.scalar(query)
        if job is None:
            return None
        if job.attempt >= _MAX_ATTEMPTS:
            _fail(db, job, '归档进程多次中断，请重新发起归档')
            return None
        job.status = Archive.RECONCILING
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=3)
        dataset = db.get(Dataset, job.dataset_id)
        return (job.id, job.attempt, dataset.id, dataset.rows,
                dataset.archive_rows, job.switched)


def switch(lease):
    """阶段一：人数核对一致后一次性切换到归档人数。"""
    job_id, attempt, dataset_id, rows, archive_rows, switched = lease
    try:
        if not switched:
            if archive_rows is None:
                archive_rows, summary = build_archive(rows)
                # 原始月度归属与归档重建结果必须逐项一致，否则中止且不写任何归档数据。
                reconcile(rows, archive_rows)
            else:
                # 上次中断在切换事务之后：以已存聚合复核原批，不再重新生成。
                summary = reconcile(rows, archive_rows)
            with Session.begin() as db:
                job = db.get(ArchiveJob, job_id, with_for_update=True)
                if job is None or job.attempt != attempt or job.status != Archive.RECONCILING:
                    return
                dataset = db.get(Dataset, dataset_id, with_for_update=True)
                if not job.switched:
                    if dataset.archive_rows is None:
                        dataset.archive_rows = archive_rows
                    if dataset.rows is not None:
                        # 落库前再核对一次，挡住任何并发写入造成的偏差。
                        reconcile(dataset.rows, dataset.archive_rows)
                    job.switched = True
                    dataset.archive_status = Archive.SWITCHED
                    dataset.archived_at = now()
                    dataset.archive_note = '新任务已改读归档人数，等待存量任务结束后清除原明细'
                    record(db, 'worker', 'archive.switched', dataset_id, job_id=job_id,
                           attempt=attempt, **summary)
                # 先落到 SWITCHED，是否进入 PURGING/COMPLETE 交给 purge_ready 统一判定。
                job.status = Archive.SWITCHED
                job.lease_until = None
                job.updated_at = now()
        purge_ready(job_id)
    except ValueError as failure:
        _mark_failed(job_id, attempt, str(failure))
    except Exception:
        log.exception('Archive %s switch failed', job_id)
        _mark_failed(job_id, attempt, '归档核对失败，请检查源明细后重试')


def purge_ready(job_id):
    """阶段二：无引用中的运行时清除原明细，否则保持等待，可重复安全调用。"""
    with Session.begin() as db:
        job = db.get(ArchiveJob, job_id, with_for_update=True)
        if job is None or job.purged or job.status == Archive.COMPLETE:
            return
        if job.status not in (Archive.PURGING, Archive.SWITCHED):
            return
        dataset = db.get(Dataset, job.dataset_id, with_for_update=True)
        active = db.scalar(select(func.count()).select_from(Run).where(
            Run.dataset_id == dataset.id, Run.status.in_(_ACTIVE_RUN_STATES)))
        if active:
            if job.status != Archive.PURGING:
                job.status = Archive.PURGING
                dataset.archive_status = Archive.PURGING
                dataset.archive_note = f'有 {active} 个引用原批的任务尚未结束，明细暂予保留'
                record(db, 'worker', 'archive.waiting', dataset.id, job_id=job_id, active_runs=active)
            job.lease_until = None
            job.updated_at = now()
            return
        removed = dataset.row_count
        dataset.rows = None
        dataset.archive_status = Archive.COMPLETE
        dataset.archive_note = '逐笔明细已清除，仅保留月度人数聚合，月报仍可按任意月份和门槛重算'
        job.purged = True
        job.status = Archive.COMPLETE
        job.lease_until = None
        job.updated_at = now()
        record(db, 'worker', 'archive.complete', dataset.id, job_id=job_id, removed_rows=removed)


def materialize_waiting():
    """每个 worker 周期复查已切换、待清除的归档，不占用租约、不消耗重试次数。

    SWITCHED 也纳入复查：切换事务提交后、首次清除检查前若进程崩溃，
    恢复时同样由这里安全推进，且 switched 标记保证不会重复切换。
    """
    with Session.begin() as db:
        jobs = db.scalars(select(ArchiveJob).where(
            ArchiveJob.status.in_((Archive.SWITCHED, Archive.PURGING)))
            .order_by(ArchiveJob.id).limit(20).with_for_update(skip_locked=True))
        for job in jobs:
            purge_ready(job.id)


def _mark_failed(job_id, attempt, reason):
    with Session.begin() as db:
        job = db.get(ArchiveJob, job_id, with_for_update=True)
        if job is None or job.attempt != attempt or job.status != Archive.RECONCILING:
            return
        _fail(db, job, reason[:500])


def _fail(db, job, reason):
    job.status = Archive.FAILED
    job.error = reason
    job.lease_until = None
    job.updated_at = now()
    dataset = db.get(Dataset, job.dataset_id)
    if dataset is not None and dataset.archive_status != Archive.COMPLETE:
        dataset.archive_status = Archive.FAILED
        dataset.archive_note = reason
    record(db, 'worker', 'archive.failed', job.dataset_id, job_id=job.id, reason=reason)
