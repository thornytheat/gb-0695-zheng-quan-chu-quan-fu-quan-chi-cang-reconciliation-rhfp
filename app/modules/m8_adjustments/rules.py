"""M8 期后调整规则。

晚到记录沿用既有 CSV 结构（reader.HEADERS），只取 position 行作为补录的登记/到账依据。
对照来源批次（已关账）的冻结数据复算，产出带原期间/调整期间的调整分录；
本模块不修改来源批次的任何内容。
"""
import csv
import io
import hashlib

from app.domain import entitlement, allocate_cents
from app.numeric import number, fmt
from app.errors import DomainError
from app.config import HEADERS, RULE_VERSION
from app.adjust_store import period_of, idem_key


def _parse_late_text(raw_text):
    """解析晚到 CSV，返回 (position 行, 隔离列表)。表头规则与 M1 一致。"""
    text = raw_text.lstrip('﻿')
    if not text:
        raise DomainError('EMPTY', '晚到记录为空')
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        head = next(reader, None)
    except csv.Error:
        raise DomainError('STRUCTURE', '表头损坏')
    head = [x.strip() for x in head]
    if set(head) != set(HEADERS) or len(head) != len(HEADERS):
        raise DomainError('HEADER', '字段缺失或重复')
    positions, quarantine = [], []
    for cells in reader:
        if not cells or all(not x.strip() for x in cells) or cells == head:
            continue
        if len(cells) != len(head):
            quarantine.append({'行号': reader.line_num, '原因码': 'COLUMNS', '原因': '列数不符',
                               '原值': '|'.join(cells)})
            continue
        row = {**dict(zip(head, [x.strip() for x in cells])), '行号': reader.line_num}
        if row['kind'] != 'position':
            quarantine.append({**row, '原因码': 'STRUCTURE', '原因': '期后调整只接收持仓/到账记录'})
            continue
        # 与 M2 同一映射口径：alias 优先，前导零按字符串保留
        row['canonical'] = row['alias'] or row['security']
        positions.append(row)
    if not positions and not quarantine:
        raise DomainError('HEADER_ONLY', '文件没有业务记录')
    return positions, quarantine


def _baseline_index(source_run):
    """来源批次冻结数据建索引：(record, security) -> 已发布持仓行。"""
    index = {}
    for r in source_run.get('holdings', []):
        index[(r['持仓记录'], r['证券'])] = r
    return index


def _action_index(source_run):
    actions = {}
    for a in source_run.get('actions', []):
        actions[a['canonical']] = a
    return actions


def build_adjustment(late_text, source_run, adjust_period):
    """构造调整单（未落库）。source_run 必须是 get_run 读出的冻结批次。"""
    positions, quarantine = _parse_late_text(late_text)
    baseline = _baseline_index(source_run)
    actions = _action_index(source_run)
    source_sha = hashlib.sha256(late_text.encode('utf-8')).hexdigest()
    source_run_id = source_run['manifest']['run_id']

    # 现金尾差沿用 M5：按证券分组后一次性最大余数分配
    raw_entries = []
    for p in positions:
        origin = period_of(p['event_date'])
        old = baseline.get((p['record'], p['canonical']))
        action = actions.get(p['canonical'])
        if action is None:
            quarantine.append({**p, '原因码': 'CONFLICT',
                               '原因': f"来源批次无证券 {p['canonical']} 的公司行动，无法复算"})
            continue
        calc = entitlement(p, action)
        raw_entries.append({'position': p, 'action': action, 'origin': origin,
                            'old': old, 'calc': calc})

    # 分组分配分红到分
    cash_inputs = {x['position']['record']: x['calc']['cash'] for x in raw_entries}
    allocated = allocate_cents(cash_inputs) if cash_inputs else {}

    entries, seen_keys = [], set()
    for x in raw_entries:
        p, action, origin, old, calc = x['position'], x['action'], x['origin'], x['old'], x['calc']
        adjusted_cash = allocated[p['record']]
        adjusted_shares = calc['new_shares']
        if old is None:
            entry_type = '新增派发'
            origin_shares, origin_cash = '0', fmt(0, '0.01')
            share_delta = adjusted_shares
            cash_delta = adjusted_cash
        else:
            origin_shares = old['新股份']
            origin_cash = old['分红_CNY']
            share_delta = adjusted_shares - number(origin_shares)
            cash_delta = adjusted_cash - number(origin_cash)
            if share_delta != 0:
                entry_type = '权益修订'
            elif cash_delta != 0:
                entry_type = '现金补差'
            else:
                entry_type = '无差异'
        digest = hashlib.sha256(
            '|'.join([p['shares'], p.get('reported_cash', ''), action['version']]).encode('utf-8')
        ).hexdigest()
        key = idem_key(source_run_id, p['record'], p['canonical'], p['event_date'], digest)
        if key in seen_keys:
            quarantine.append({**p, '原因码': 'DUPLICATE', '原因': '同一晚到记录在文件内重复'})
            continue
        seen_keys.add(key)
        # 与已生效/已登记分录的同键冲突在落库层拦截；这里先给业务键内容冲突留痕
        entries.append({
            'idem_key': key,
            'source_run_id': source_run_id,
            'record_id': p['record'],
            'security': p['canonical'],
            'event_date': p['event_date'],
            'entry_type': entry_type,
            'origin_period': origin,
            'adjust_period': adjust_period,
            'origin_shares': str(origin_shares),
            'adjusted_shares': str(adjusted_shares),
            'share_delta': str(share_delta),
            'origin_cash': fmt(number(origin_cash), '0.01'),
            'adjusted_cash': fmt(adjusted_cash, '0.01'),
            'cash_delta': fmt(cash_delta, '0.01'),
            'source_line': p['行号'],
            'source_sha256': source_sha,
        })

    batch_id = hashlib.sha256(
        (RULE_VERSION + ':' + source_run_id + ':' + adjust_period + ':' + source_sha).encode('utf-8')
    ).hexdigest()[:16]
    share_total = sum((number(e['share_delta']) for e in entries), number('0'))
    cash_total = sum((number(e['cash_delta']) for e in entries), number('0'))
    return {
        'id': batch_id,
        'source_run_id': source_run_id,
        'origin_period': min((e['origin_period'] for e in entries), default=adjust_period),
        'adjust_period': adjust_period,
        'source_sha256': source_sha,
        'rule_version': RULE_VERSION,
        'entries': entries,
        'quarantine': quarantine,
        'summary': {
            'entries': len(entries),
            'quarantined': len(quarantine),
            'share_delta_total': str(share_total),
            'cash_delta_total': fmt(cash_total, '0.01'),
            'types': sorted({e['entry_type'] for e in entries}),
        },
    }
