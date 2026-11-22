from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Archive, Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.source import ArchiveJob, Dataset
from src.services import archives
from src.services.audit import record
from src.utils import views
from src.utils.wakeup import wake

router = APIRouter(prefix='/datasets/{dataset_id}/archive')


def _job_view(db, job):
    view = views.archive_job(job)
    dataset = db.get(Dataset, job.dataset_id)
    view['dataset_name'] = dataset.name if dataset else None
    return view


@router.get('')
def progress(dataset_id: int, db: Session = Depends(connection),
             actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    job = db.scalar(select(ArchiveJob).where(ArchiveJob.dataset_id == dataset_id))
    if job is None:
        raise HTTPException(404, '该批次还没有归档任务')
    return _job_view(db, job)


@router.post('', status_code=202)
def start(dataset_id: int, db: Session = Depends(connection),
          actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        job, summary, _ = archives.start(db, dataset_id, actor.username)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该批次已有归档任务，可在归档进度中查看') from None
    db.commit()
    wake()
    return {**_job_view(db, job), 'summary': summary}


@router.post('/retry', status_code=202)
def retry(dataset_id: int, db: Session = Depends(connection),
          actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    job = db.scalar(select(ArchiveJob).where(ArchiveJob.dataset_id == dataset_id)
                    .with_for_update())
    if job is None:
        raise HTTPException(404, '该批次还没有归档任务')
    if job.status != Archive.FAILED:
        raise HTTPException(409, '只有失败的归档任务可以重试')
    if job.purged:
        raise HTTPException(409, '原明细已清除，无需重试')
    # 重试只重置租约与尝试次数；switched 保持不变，恢复后跳过切换、直接等清除。
    job.status = Archive.PURGING if job.switched else Archive.PENDING
    job.attempt = 0
    job.error = None
    job.lease_until = None
    job.updated_at = None
    dataset = db.get(Dataset, dataset_id, with_for_update=True)
    dataset.archive_status = job.status
    dataset.archive_note = '归档已恢复，等待后台继续处理' if job.switched else '归档已重试，等待后台重新核对'
    record(db, actor.username, 'archive.retried', dataset_id, job_id=job.id, switched=job.switched)
    db.commit()
    wake()
    return _job_view(db, job)
