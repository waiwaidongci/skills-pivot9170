import csv
import hashlib
import io
import json
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models.source import Dataset, PurgedChecksum


def checksum(rows):
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def save(db, name, filename, rows, actor):
    signature = checksum(rows)
    # 内容指纹一旦进入归档控制，改名或并发重传同一份明细都会被拒绝，不能找回已清除的记录。
    blocked = db.get(PurgedChecksum, signature)
    if blocked is not None:
        raise ValueError('该明细已归档并清除逐笔记录，按留存策略不能重新导入同内容文件。')
    lookup = select(Dataset).where(Dataset.name == name, Dataset.checksum == signature)
    existing = db.scalar(lookup)
    if existing:
        return existing, False
    dataset = Dataset(name=name, filename=filename, checksum=signature, rows=rows,
                      row_count=len(rows), author=actor)
    try:
        with db.begin_nested():
            db.add(dataset)
            db.flush()
    except IntegrityError:
        existing = db.scalar(lookup.with_for_update())
        if existing:
            return existing, False
        if db.get(PurgedChecksum, signature) is not None:
            raise ValueError('该明细已归档并清除逐笔记录，按留存策略不能重新导入同内容文件。') from None
        raise
    return dataset, True


def template():
    fields = [field['key'] for field in SPEC['fields']]
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    # The complete representative dataset also works as a reproducible import fixture.
    writer.writerows(demo_rows())
    return '\ufeff' + buffer.getvalue()
