"""归档功能的 HTTP 端到端测试：权限、发起、进度、重传防护与归档后计算。"""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix='archive-api-')
os.environ['DATABASE_URL'] = f'sqlite:///{_tmp}/api.db'
os.environ['JWT_SECRET'] = 'api-test-secret'
os.environ['SEED_DEMO'] = 'false'
os.environ['ARTIFACT_DIR'] = f'{_tmp}/artifacts'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.database import Base, engine  # noqa: E402
from src.constants.enums import Role  # noqa: E402
from src.domain.demo import demo_rows  # noqa: E402
from src.main import app  # noqa: E402
from src.models.identity import User  # noqa: E402
from src.models.source import Dataset  # noqa: E402
from src.services import archives  # noqa: E402
from src.services.datasets import save  # noqa: E402
from src.config.database import Session  # noqa: E402
from src.utils.security import hash_password  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session.begin() as db:
        db.add(User(username='admin', role=Role.ADMIN, password_hash=hash_password('pw-admin')))
        db.add(User(username='analyst', role=Role.ANALYST, password_hash=hash_password('pw-analyst')))
        db.add(User(username='viewer', role=Role.VIEWER, password_hash=hash_password('pw-viewer')))
        save(db, '演示批', 'demo.csv', demo_rows(), 'analyst')
    with TestClient(app) as test_client:
        yield test_client


def login(client, username, password):
    response = client.post('/auth/login', json={'username': username, 'password': password})
    assert response.status_code == 200
    return response.json()['token']


def auth(token):
    return {'Authorization': f'Bearer {token}'}


def test_viewer_cannot_archive(client):
    token = login(client, 'viewer', 'pw-viewer')
    response = client.post('/datasets/1/archive', headers=auth(token), json={})
    assert response.status_code == 403
    response = client.get('/datasets/1/archive', headers=auth(token))
    assert response.status_code == 403


def test_archive_lifecycle_and_progress_api(client):
    token = login(client, 'analyst', 'pw-analyst')
    # 未发起时查询进度返回 404。
    assert client.get('/datasets/1/archive', headers=auth(token)).status_code == 404

    response = client.post('/datasets/1/archive', headers=auth(token), json={})
    assert response.status_code == 202
    body = response.json()
    assert body['status'] == 'pending' and body['switched'] is False
    assert body['summary']['intake_people'] == body['summary']['archived_people']

    # 驱动 worker 阶段一（核对 + 切换），再推进阶段二（清除）。
    lease = archives.claim()
    archives.switch(lease)
    archives.materialize_waiting()

    progress = client.get('/datasets/1/archive', headers=auth(token)).json()
    assert progress['status'] == 'complete' and progress['purged'] is True

    detail = client.get('/datasets/1', headers=auth(token)).json()
    assert detail['archived'] is True and detail['archive_mode'] is True
    assert all('learner_id' not in row for row in detail['rows'])
    assert {'period', 'channel', 'program', 'intake_count', 'confirmed_count'} <= set(detail['rows'][0])

    # 重复发起被拒绝。
    again = client.post('/datasets/1/archive', headers=auth(token), json={})
    assert again.status_code == 422


def test_renamed_reupload_is_rejected_via_api(client):
    token = login(client, 'admin', 'pw-admin')
    client.post('/datasets/1/archive', headers=auth(token), json={})
    archives.switch(archives.claim())
    archives.materialize_waiting()

    # 同字节内容、改文件名和批次名重传 → 422，无法找回逐笔明细。
    response = client.post('/datasets', headers=auth(token), json={
        'name': '改名后的批次', 'filename': '榆桥职训受理_改名重传.csv',
        'csv_text': _csv_text(),
    })
    assert response.status_code == 422
    assert '不能重新导入' in response.json()['detail']
    with Session() as db:
        assert db.query(Dataset).count() == 1


def test_archived_dataset_runs_produce_identical_report(client):
    token = login(client, 'analyst', 'pw-analyst')
    # 归档前先跑一次留存基准结果。
    before = client.post('/runs', headers=auth(token), json={
        'dataset_id': 1, 'name': '归档前', 'params': {'period': '2026-10', 'min_count': 5}})
    assert before.status_code == 202
    run_id = before.json()['id']
    from src.services.execution import claim, execute
    lease = claim()
    execute(lease)
    baseline = client.get(f'/runs/{run_id}', headers=auth(token)).json()['result']

    client.post('/datasets/1/archive', headers=auth(token), json={})
    archives.switch(archives.claim())
    archives.materialize_waiting()

    after = client.post('/runs', headers=auth(token), json={
        'dataset_id': 1, 'name': '归档后', 'params': {'period': '2026-10', 'min_count': 5}})
    assert after.status_code == 202
    execute(claim())
    rerun = client.get(f'/runs/{after.json()["id"]}', headers=auth(token)).json()
    assert rerun['status'] == 'complete'
    assert rerun['result'] == baseline
    assert rerun['has_export'] is True

    # 换一个披露门槛（6 人）同样一致。
    for threshold in (6, 20):
        first = client.post('/runs', headers=auth(token), json={
            'dataset_id': 1, 'name': f'门槛{threshold}', 'params': {'period': '2026-11', 'min_count': threshold}})
        execute(claim())
        got = client.get(f'/runs/{first.json()["id"]}', headers=auth(token)).json()['result']
        from src.domain.analysis import analyze
        from src.domain.archiving import analyze_archived
        with Session() as db:
            ds = db.get(Dataset, 1)
            assert analyze_archived(ds.archive_rows, {'period': '2026-11', 'min_count': threshold}) == got


def _csv_text():
    import csv
    import io
    buffer = io.StringIO()
    rows = demo_rows()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, 'enrolled': 'true' if row['enrolled'] else 'false'})
    return buffer.getvalue()
