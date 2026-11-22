"""归档功能的可重算性、状态机与重传防护测试。

运行：在 backend 目录下用隔离环境执行
    python -m pytest tests/test_archiving.py -q
使用 SQLite 内存库，无需 MySQL / Redis / Docker。
"""

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

_tmp = tempfile.mkdtemp(prefix='archive-test-')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_tmp}/test.db')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', f'{_tmp}/artifacts')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.database import Base  # noqa: E402
from src.constants.enums import Archive, Status  # noqa: E402
from src.domain.analysis import analyze  # noqa: E402
from src.domain.archiving import analyze_archived, build_archive, reconcile  # noqa: E402
from src.domain.demo import demo_rows  # noqa: E402
from src.models.jobs import Run  # noqa: E402
from src.models.source import ArchiveJob, Dataset, PurgedChecksum  # noqa: E402
from src.services import archives  # noqa: E402
from src.services.datasets import save  # noqa: E402
from src.services.execution import claim as claim_run, execute, load_payload  # noqa: E402
from src.utils.time import now  # noqa: E402

engine = create_engine(os.environ['DATABASE_URL'], connect_args={'check_same_thread': False})
Session = sessionmaker(engine, expire_on_commit=False)


@pytest.fixture()
def db_session():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session.begin() as db:
        yield db


@pytest.fixture()
def dataset(db_session):
    item, created = save(db_session, '演示批', 'demo.csv', demo_rows(), 'analyst')
    assert created
    return item


def _analyze(rows, period, threshold):
    return analyze(rows, {'period': period, 'min_count': threshold})


def test_archive_reproduces_any_month_and_threshold(dataset):
    """原批与归档人数，在任意月份/门槛下结果完全一致。"""
    archived_rows, summary = build_archive(dataset.rows)
    assert summary['intake_people'] == summary['archived_people']
    reconcile(dataset.rows, archived_rows)
    for period in ('2026-10', '2026-11'):
        for threshold in (5, 6, 20, 100):
            params = {'period': period, 'min_count': threshold}
            assert analyze(dataset.rows, params) == analyze_archived(archived_rows, params)


def test_full_archive_flow_switch_wait_purge(dataset):
    with Session.begin() as db:
        job, summary, _ = archives.start(db, dataset.id, 'analyst')
        job_id = job.id
        assert summary['intake_people'] > 0
        assert db.get(PurgedChecksum, dataset.checksum) is not None

    # 一个引用原批的运行先占住，归档应停在「等待清除」且保留 rows。
    with Session.begin() as db:
        run = Run(dataset_id=dataset.id, name='核对运行', params={'period': '2026-11', 'min_count': 5},
                  status=Status.QUEUED, author='analyst')
        db.add(run)

    lease = archives.claim()
    assert lease is not None
    archives.switch(lease)
    with Session() as db:
        job = db.get(ArchiveJob, job_id)
        ds = db.get(Dataset, dataset.id)
        assert job.switched is True and job.purged is False
        assert job.status == Archive.PURGING
        assert ds.archive_status == Archive.PURGING
        assert ds.rows is not None and ds.archive_rows is not None

    # 等待期间可反复复查，不产生重复切换、不报错。
    archives.materialize_waiting()
    archives.materialize_waiting()
    with Session() as db:
        assert db.get(ArchiveJob, job_id).switched is True
        assert db.get(Dataset, dataset.id).rows is not None

    # 存量任务结束后，复查即清除原明细。
    with Session.begin() as db:
        run = db.scalar(select(Run).where(Run.dataset_id == dataset.id))
        run.status = Status.COMPLETE
        run.finished_at = now()
    archives.materialize_waiting()
    with Session() as db:
        job = db.get(ArchiveJob, job_id)
        ds = db.get(Dataset, dataset.id)
        assert job.purged is True and job.status == Archive.COMPLETE
        assert ds.archive_status == Archive.COMPLETE and ds.rows is None and ds.archive_rows is not None

    # 清除后新运行读取归档人数，月报与原批结果一致。
    with Session.begin() as db:
        new_run = Run(dataset_id=dataset.id, name='归档后运行',
                      params={'period': '2026-10', 'min_count': 5}, author='analyst')
        db.add(new_run)
        db.flush()
        run_id = new_run.id
    run_lease = claim_run()
    assert run_lease[0] == run_id
    execute(run_lease)
    with Session() as db:
        finished = db.get(Run, run_id)
        assert finished.status == Status.COMPLETE
        assert finished.result == _analyze(dataset.rows, '2026-10', 5)
        assert finished.export_file


def test_rename_reupload_cannot_recover_purged_rows(dataset):
    with Session.begin() as db:
        archives.start(db, dataset.id, 'analyst')
    lease = archives.claim()
    archives.switch(lease)
    archives.materialize_waiting()
    with Session() as db:
        assert db.get(Dataset, dataset.id).rows is None

    # 同字节内容改名重传（含并发场景下的 savepoint 竞争路径）均被拒绝。
    with Session.begin() as db:
        with pytest.raises(ValueError, match='不能重新导入'):
            save(db, '另一个批次名', '改名文件.csv', demo_rows(), 'analyst')
        assert db.scalar(select(func.count()).select_from(Dataset)) == 1


def test_retry_after_interruption_does_not_switch_twice(dataset):
    with Session.begin() as db:
        job, _, _ = archives.start(db, dataset.id, 'analyst')
        job_id = job.id

    first = archives.claim()
    archives.switch(first)
    with Session() as db:
        assert db.get(ArchiveJob, job_id).switched is True

    # 模拟 worker 在 PURGING 阶段中断后，租约型 claim 不再领取该任务，只能由复查推进。
    assert archives.claim() is None
    archives.materialize_waiting()
    with Session() as db:
        job = db.get(ArchiveJob, job_id)
        assert job.purged is True and job.attempt == 1
        ds = db.get(Dataset, dataset.id)
        assert ds.rows is None

    # 对账失败路径：中断在切换前（switched=False），重试从头核对，不会写入半份聚合。
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session.begin() as db:
        ds, _ = save(db, '演示批', 'demo.csv', demo_rows(), 'analyst')
        archives.start(db, ds.id, 'analyst')
    lease = archives.claim()
    # 构造一份与原批月度归属不一致的聚合（多出 1 人），核对必须失败且不写入 archive_rows。
    bad_archive, _ = build_archive(demo_rows())
    bad_archive[0]['intake_count'] += 1
    archives.switch((lease[0], lease[1], ds.id, demo_rows(), bad_archive, False))
    with Session() as db:
        job = db.get(ArchiveJob, ds.id)
        assert job.status == Archive.FAILED
        assert db.get(Dataset, ds.id).archive_rows is None


def test_concurrent_rename_reuploads_both_rejected(dataset):
    """两个并发请求同时重传已归档内容，都不能落出可找回的明细批。"""
    import threading
    with Session.begin() as db:
        archives.start(db, dataset.id, 'analyst')
    archives.switch(archives.claim())
    archives.materialize_waiting()

    outcomes = []

    def worker():
        try:
            with Session.begin() as db:
                save(db, f'并发批-{threading.get_ident()}', '重传.csv', demo_rows(), 'analyst')
            outcomes.append('created')
        except ValueError:
            outcomes.append('rejected')
        except Exception as error:  # pragma: no cover - 仅暴露意外错误
            outcomes.append(f'error:{type(error).__name__}')

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == ['rejected', 'rejected']
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Dataset)) == 1


def test_reconcile_rejects_tampered_aggregate(dataset):
    archived_rows, _ = build_archive(dataset.rows)
    archived_rows[0]['intake_count'] += 1
    with pytest.raises(ValueError, match='核对不一致'):
        reconcile(dataset.rows, archived_rows)


def test_retry_endpoint_resumes_without_duplicate_switch(dataset):
    """失败后重试：已切换过的只回到 PURGING，switched 不复位。"""
    with Session.begin() as db:
        job, _, _ = archives.start(db, dataset.id, 'analyst')
        job_id = job.id
    archives.switch(archives.claim())
    with Session.begin() as db:
        job = db.get(ArchiveJob, job_id, with_for_update=True)
        job.status = Archive.FAILED
        job.error = '模拟中断'
        ds = db.get(Dataset, dataset.id)
        ds.archive_status = Archive.FAILED
    with Session.begin() as db:
        job = db.get(ArchiveJob, job_id, with_for_update=True)
        job.status = Archive.PURGING if job.switched else Archive.PENDING
        job.attempt = 0
        job.error = None
        job.lease_until = None
    assert archives.claim() is None  # 已切换的不会被重新核对
    archives.materialize_waiting()
    with Session() as db:
        job = db.get(ArchiveJob, job_id)
        assert job.switched is True and job.purged is True
        assert db.get(Dataset, dataset.id).rows is None


def test_crash_between_switch_and_purge_resumes_safely(dataset, monkeypatch):
    """切换事务提交后、首次清除检查前崩溃：恢复后由复查推进，不重复切换。"""
    with Session.begin() as db:
        job, _, _ = archives.start(db, dataset.id, 'analyst')
        job_id = job.id
    monkeypatch.setattr(archives, 'purge_ready', lambda _id: None)
    archives.switch(archives.claim())
    monkeypatch.undo()

    with Session() as db:
        job = db.get(ArchiveJob, job_id)
        assert job.status == Archive.SWITCHED and job.switched is True and job.purged is False
    # 租约领取不会再碰它，恢复只能走复查通道。
    assert archives.claim() is None
    archives.materialize_waiting()
    with Session() as db:
        job = db.get(ArchiveJob, job_id)
        assert job.purged is True and job.status == Archive.COMPLETE
        assert db.get(Dataset, dataset.id).rows is None


def test_lease_expiry_allows_reclaim_but_keeps_single_switch(dataset):
    """租约过期可被重新领取；切换事务的 switched 标记保证只切换一次。"""
    with Session.begin() as db:
        job, _, _ = archives.start(db, dataset.id, 'analyst')
        job_id = job.id
    first = archives.claim()
    # 第一次 worker 在核对后、提交前崩溃：租约过期后重新领取。
    with Session.begin() as db:
        stale = db.get(ArchiveJob, job_id, with_for_update=True)
        stale.lease_until = now() - timedelta(minutes=10)
    second = archives.claim()
    assert second is not None and second[1] == 2
    archives.switch(second)
    with Session() as db:
        job = db.get(ArchiveJob, job_id)
        assert job.switched is True
        assert job.attempt == 2
    archives.materialize_waiting()
    with Session() as db:
        assert db.get(ArchiveJob, job_id).purged is True


def test_load_payload_uses_archive_after_purge(dataset):
    with Session.begin() as db:
        archives.start(db, dataset.id, 'analyst')
    archives.switch(archives.claim())
    archives.materialize_waiting()
    with Session() as db:
        ds = db.get(Dataset, dataset.id)
        source, payload = load_payload(ds)
        assert source == 'archive'
        assert analyze_archived(payload, {'period': '2026-11', 'min_count': 5}) \
            == _analyze(dataset.rows, '2026-11', 5)
        # 归档负载只含人数，不含任何学员编号。
        assert all('learner_id' not in row for row in payload)
