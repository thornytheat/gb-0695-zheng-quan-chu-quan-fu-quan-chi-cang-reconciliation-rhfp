"""期后调整存储：期间状态机 + 调整单 + 幂等分录。

与 app/store.py 的 runs 表完全独立：关账批次的 payload 永不被本模块改写。
"""
import sqlite3
import json
import os
import hashlib
import time
from pathlib import Path

from app.errors import DomainError
from app.config import RULE_VERSION

__all__ = ['DomainError', 'period_of', 'idem_key']

# 期间按登记/快照日的年月归属，半开边界：[月初, 下月初)
def period_of(day_text):
    return day_text[:7]


def idem_key(source_run_id, record, security, event_date, digest):
    raw = '|'.join([RULE_VERSION, source_run_id or '', str(record), str(security), str(event_date), digest])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _connection():
    path = Path(os.environ.get('APP_DB', 'data/runs.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute(
        'CREATE TABLE IF NOT EXISTS periods('
        'period TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT "开放", '
        'closed_at TEXT, revision INTEGER NOT NULL DEFAULT 1)'
    )
    db.execute(
        'CREATE TABLE IF NOT EXISTS adjustment_batches('
        'id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL, origin_period TEXT NOT NULL, '
        'adjust_period TEXT NOT NULL, source_sha256 TEXT NOT NULL, rule_version TEXT NOT NULL, '
        'payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, '
        'decision TEXT NOT NULL DEFAULT "待复核", created_at TEXT NOT NULL)'
    )
    db.execute(
        'CREATE TABLE IF NOT EXISTS adjustment_entries('
        'idem_key TEXT PRIMARY KEY, batch_id TEXT NOT NULL, source_run_id TEXT NOT NULL, '
        'record_id TEXT NOT NULL, security TEXT NOT NULL, event_date TEXT NOT NULL, '
        'entry_type TEXT NOT NULL, origin_period TEXT NOT NULL, adjust_period TEXT NOT NULL, '
        'share_delta TEXT NOT NULL, cash_delta TEXT NOT NULL, '
        'origin_shares TEXT NOT NULL, adjusted_shares TEXT NOT NULL, '
        'origin_cash TEXT NOT NULL, adjusted_cash TEXT NOT NULL, '
        'source_line INTEGER, source_sha256 TEXT NOT NULL, reason_code TEXT, '
        'created_at TEXT NOT NULL)'
    )
    return db


def ensure_period(db, period):
    db.execute(
        'INSERT OR IGNORE INTO periods(period) VALUES(?)', (period,)
    )


def get_period(period):
    with _connection() as db:
        ensure_period(db, period)
        row = db.execute('SELECT * FROM periods WHERE period=?', (period,)).fetchone()
        return dict(row)


def close_period(period, revision):
    """关账带乐观锁；已关账再次关账返回 409 语义错误。"""
    with _connection() as db:
        ensure_period(db, period)
        row = db.execute('SELECT status,revision FROM periods WHERE period=?', (period,)).fetchone()
        if row['status'] == '已关账':
            raise DomainError('PERIOD_CLOSED', '期间已关账，不能重复关账', 409)
        changed = db.execute(
            'UPDATE periods SET status="已关账", closed_at=?, revision=revision+1 '
            'WHERE period=? AND revision=?',
            (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), period, revision),
        ).rowcount
        if not changed:
            raise DomainError('VERSION', '期间版本已变化，请重新载入', 409)
    return {'period': period, 'status': '已关账', 'revision': revision + 1}


def list_periods():
    with _connection() as db:
        return [dict(r) for r in db.execute('SELECT * FROM periods ORDER BY period DESC')]


def save_adjustment(batch):
    """幂等落库。返回 (batch_dict, reused)。

    - 整单同文（source_sha256 + 调整期间）已存在：复用，不新增分录。
    - 新单：目标调整期间必须开放；分录按 idem_key 去重，冲突键报错由调用方隔离。
    """
    adjust_period = batch['adjust_period']
    with _connection() as db:
        ensure_period(db, adjust_period)
        ensure_period(db, batch['origin_period'])
        status = db.execute('SELECT status FROM periods WHERE period=?', (adjust_period,)).fetchone()['status']
        if status == '已关账':
            raise DomainError('PERIOD_CLOSED', f'调整期间 {adjust_period} 已关账，晚到记录不能入账', 422)

        existing = db.execute(
            'SELECT id,revision,decision,payload FROM adjustment_batches '
            'WHERE source_sha256=? AND adjust_period=?',
            (batch['source_sha256'], adjust_period),
        ).fetchone()
        if existing:
            return {**json.loads(existing['payload']), 'revision': existing['revision'],
                    'decision': existing['decision'], 'reused': True}, True

        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        # 落库前显式识别全局已存在的幂等键：同一笔晚到记录无论再投到哪个期间都只生效一次
        if batch['entries']:
            placeholders = ','.join('?' for _ in batch['entries'])
            known = {r['idem_key'] for r in db.execute(
                f'SELECT idem_key FROM adjustment_entries WHERE idem_key IN ({placeholders})',
                [e['idem_key'] for e in batch['entries']])}
        else:
            known = set()
        fresh, deduped = [], []
        for e in batch['entries']:
            (deduped if e['idem_key'] in known else fresh).append(e)
        for e in deduped:
            batch.setdefault('quarantine', []).append(
                {'行号': e['source_line'], 'record': e['record_id'], '原因码': 'DEDUPED',
                 '原因': f"幂等键 {e['idem_key']} 已在调整期间 {e['adjust_period']} 之外的单据生效，本次不重复入账",
                 'idem_key': e['idem_key']})
        batch['entries'] = fresh
        batch['summary']['entries'] = len(fresh)
        batch['summary']['quarantined'] = len(batch.get('quarantine', []))
        db.execute(
            'INSERT INTO adjustment_batches(id,source_run_id,origin_period,adjust_period,'
            'source_sha256,rule_version,payload,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (batch['id'], batch['source_run_id'], batch['origin_period'], adjust_period,
             batch['source_sha256'], batch['rule_version'],
             json.dumps(batch, ensure_ascii=False), now),
        )
        for e in fresh:
            db.execute(
                'INSERT OR IGNORE INTO adjustment_entries(idem_key,batch_id,source_run_id,'
                'record_id,security,event_date,entry_type,origin_period,adjust_period,'
                'share_delta,cash_delta,origin_shares,adjusted_shares,origin_cash,adjusted_cash,'
                'source_line,source_sha256,reason_code,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (e['idem_key'], batch['id'], e['source_run_id'], e['record_id'], e['security'],
                 e['event_date'], e['entry_type'], e['origin_period'], e['adjust_period'],
                 e['share_delta'], e['cash_delta'], e['origin_shares'], e['adjusted_shares'],
                 e['origin_cash'], e['adjusted_cash'], e['source_line'], e['source_sha256'],
                 e.get('reason_code'), now),
            )
    return {**batch, 'revision': 1, 'decision': '待复核', 'reused': False}, False


def get_batch(batch_id):
    with _connection() as db:
        row = db.execute('SELECT payload,revision,decision FROM adjustment_batches WHERE id=?',
                         (batch_id,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '调整单不存在', 404)
        return {**json.loads(row['payload']), 'revision': row['revision'], 'decision': row['decision']}


def list_batches():
    with _connection() as db:
        return [dict(r) for r in db.execute(
            'SELECT id,source_run_id,origin_period,adjust_period,revision,decision,created_at '
            'FROM adjustment_batches ORDER BY rowid DESC')]


def review_batch(batch_id, revision, decision):
    if decision not in ('通过', '退回'):
        raise DomainError('DECISION', '复核动作无效')
    with _connection() as db:
        row = db.execute('SELECT decision FROM adjustment_batches WHERE id=?', (batch_id,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '调整单不存在', 404)
        if row['decision'] == '通过':
            # 已生效调整单锁定：不能再改决策，保证同一笔不会被二次生效或撤销后重发
            raise DomainError('LOCKED', '调整单已生效，复核决策锁定', 409)
        changed = db.execute(
            'UPDATE adjustment_batches SET revision=revision+1,decision=? '
            'WHERE id=? AND revision=?', (decision, batch_id, revision)
        ).rowcount
        if not changed:
            raise DomainError('VERSION', '版本已变化，请重新载入', 409)
    return {'id': batch_id, 'revision': revision + 1, 'decision': decision}


def effective_entries(period=None):
    """生效分录（仅复核通过的调整单）。供对账视图证明每笔只计一次。"""
    sql = ('SELECT e.* FROM adjustment_entries e JOIN adjustment_batches b '
           'ON e.batch_id=b.id WHERE b.decision="通过"')
    params = ()
    if period:
        sql += ' AND e.adjust_period=?'
        params = (period,)
    with _connection() as db:
        return [dict(r) for r in db.execute(sql, params)]
