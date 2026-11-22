import json
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Role
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.source import Dataset
from src.services.datasets import save
from src.utils.security import hash_password


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        try:
            item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed')
        except ValueError:
            # 演示批已归档清除：内容指纹禁止重导，播种视为无新增，保持幂等。
            item = db.scalar(select(Dataset).where(Dataset.name == SPEC['default_dataset'])
                             .order_by(Dataset.id).limit(1))
            created = False
        return {'users': db.scalar(select(func.count()).select_from(User)),
                'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                'created_dataset': created}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
