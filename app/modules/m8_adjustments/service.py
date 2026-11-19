"""M8 期后调整服务层：组装调整单并落库，返回带两期差异的复核视图数据。"""
from app.modules.m8_adjustments.rules import build_adjustment
from app import adjust_store
from app.numeric import serial
from app.reader import decode
from app.store import get_run
from app.errors import DomainError


def submit(late_csv=None, source_run_id=None, adjust_period=None, raw_bytes=None):
    if raw_bytes is not None:
        late_text = decode(raw_bytes)
    elif late_csv is not None:
        if not isinstance(late_csv, str):
            raise DomainError('TYPE', 'CSV必须是文本')
        late_text = late_csv
    else:
        raise DomainError('EMPTY', '晚到记录为空')
    if not source_run_id:
        raise DomainError('MISSING', '必须指定来源批次 source_run_id（关账依据）')
    source_run = get_run(source_run_id)
    if source_run.get('decision') != '通过':
        raise DomainError('NOT_CLOSED', '来源批次尚未复核通过，不能作为关账依据接收期后调整', 422)
    if not adjust_period:
        raise DomainError('MISSING', '必须指定调整期间 adjust_period（YYYY-MM）')
    if len(adjust_period) != 7 or adjust_period[4] != '-':
        raise DomainError('PERIOD', '调整期间格式应为 YYYY-MM')

    batch = build_adjustment(late_text, source_run, adjust_period)
    saved, reused = adjust_store.save_adjustment(batch)
    # 附带来源批次的冻结状态，便于页面证明旧依据未被改写
    saved['source_decision'] = source_run['decision']
    saved['source_revision'] = source_run['revision']
    return serial(saved), reused


def detail(batch_id):
    return serial(adjust_store.get_batch(batch_id))


def review(batch_id, revision, decision):
    return adjust_store.review_batch(batch_id, revision, decision)
