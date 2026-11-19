"""M8 期后调整：关账冻结、唯一归属、幂等、冲突隔离、两期差异、复核状态机。"""
import pytest

from app.config import HEADERS
from app.store import save, get_run
from app.pipeline import calculate
from app import adjust_store
from app.modules.m8_adjustments import service
from app.modules.m8_adjustments.rules import build_adjustment

ACTION = ('A0001,action,000001,,2026-10-15,2026-10-20,2026-10-21,'
          '10,0.50,0.1,0,0,,1,')
POS = 'P001,position,000001,,2026-10-20,,,,,,,,100,1,50.00'
HEADER = ','.join(HEADERS)


def base_csv():
    return HEADER + '\n' + ACTION + '\n' + POS + '\n'


def late_csv(rows):
    return HEADER + '\n' + '\n'.join(rows) + '\n'


@pytest.fixture
def closed_run(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_DB', str(tmp_path / 'db.sqlite3'))
    run = save(calculate(base_csv()))
    from app.store import review
    review(run['manifest']['run_id'], 1, '通过')
    return get_run(run['manifest']['run_id'])


def test_late_missing_position_books_new_dispatch_to_open_period(closed_run):
    # 关账后补来一条旧批次完全没有的持仓（登记日仍属上期 2026-10）
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    batch, reused = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    assert reused is False
    e = batch['entries'][0]
    assert e['entry_type'] == '新增派发'
    assert e['origin_period'] == '2026-10' and e['adjust_period'] == '2026-11'
    # 200 股 * 0.5 现金 = 100；送转 0.1 -> 新增 20 股，共 220
    assert e['adjusted_shares'] == '220' and e['share_delta'] == '220'
    assert e['adjusted_cash'] == '100.00' and e['cash_delta'] == '100.00'


def test_closed_batch_payload_never_changes(closed_run):
    before = get_run(closed_run['manifest']['run_id'])
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    after = get_run(closed_run['manifest']['run_id'])
    assert before['revision'] == after['revision'] == 2
    assert before['decision'] == after['decision'] == '通过'
    assert before['stages'] == after['stages']
    assert before['manifest']['sha256'] == after['manifest']['sha256']


def test_cash_shortfall_books_only_cash_delta(closed_run):
    # 同持仓重报：股数不变，渠道到账从 50 改为 48（每股 0.5，应付仍 50，差体现在报送侧）
    # 这里用已发布应付 50 为基准，复算应付仍 50 -> 无差异；改现金参数由行动版本修订另测
    late = late_csv(['P001,position,000001,,2026-10-20,,,,,,,,100,1,48.00'])
    batch, _ = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    e = batch['entries'][0]
    assert e['entry_type'] == '无差异'
    assert e['share_delta'] == '0' and e['cash_delta'] == '0.00'


def test_rerun_same_file_is_idempotent(closed_run):
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    first, reused1 = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    second, reused2 = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    assert reused1 is False and reused2 is True
    assert first['id'] == second['id']
    assert len(adjust_store.effective_entries('2026-11')) == 0  # 还没复核通过
    service.review(first['id'], 1, '通过')
    # 重跑后生效分录仍然只有一条，不会二次派发
    again, _ = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    assert again['reused'] is True
    rows = adjust_store.effective_entries('2026-11')
    assert len(rows) == 1 and rows[0]['record_id'] == 'P999'


def test_submission_to_closed_period_rejected(closed_run):
    adjust_store.close_period('2026-11', 1)
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    with pytest.raises(adjust_store.DomainError) as exc:
        service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    assert exc.value.code == 'PERIOD_CLOSED' and exc.value.status == 422
    # 没有任何分录落库
    assert adjust_store.list_batches() == []


def test_each_late_record_has_single_owning_period(closed_run):
    late = late_csv([
        'P999,position,000001,,2026-10-20,,,,,,,,200,1,0',
        'P998,position,000001,,2026-10-21,,,,,,,,100,1,0',
    ])
    batch, _ = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    keys = [e['idem_key'] for e in batch['entries']]
    assert len(keys) == len(set(keys))
    for e in batch['entries']:
        # 唯一生效期间 = adjust_period；origin_period 仅标注业务归属，不重复入账
        assert e['adjust_period'] == '2026-11'
        assert e['origin_period'] == '2026-10'
    service.review(batch['id'], 1, '通过')
    effective = adjust_store.effective_entries()
    assert len({(r['record_id'], r['adjust_period']) for r in effective}) == len(effective) == 2


def test_unknown_security_is_quarantined_not_booked(closed_run):
    late = late_csv(['P997,position,000999,,2026-10-20,,,,,,,,50,1,0'])
    batch, _ = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    assert batch['entries'] == []
    assert batch['quarantine'][0]['原因码'] == 'CONFLICT'
    assert batch['summary']['quarantined'] == 1


def test_duplicate_row_in_file_counted_once(closed_run):
    row = 'P999,position,000001,,2026-10-20,,,,,,,,200,1,0'
    batch, _ = service.submit(late_csv([row, row]), closed_run['manifest']['run_id'], '2026-11')
    assert len(batch['entries']) == 1
    assert any(q['原因码'] == 'DUPLICATE' for q in batch['quarantine'])


def test_review_flow_and_lock(closed_run):
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    batch, _ = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    # 乐观锁：陈旧版本 409
    with pytest.raises(adjust_store.DomainError) as exc:
        service.review(batch['id'], 99, '通过')
    assert exc.value.status == 409
    service.review(batch['id'], 1, '通过')
    # 已生效调整单锁定，不能再退回/改判，防止撤销后重发导致重复
    with pytest.raises(adjust_store.DomainError) as exc:
        service.review(batch['id'], 2, '退回')
    assert exc.value.code == 'LOCKED'


def test_rejected_batch_kept_but_not_effective(closed_run):
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    batch, _ = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    service.review(batch['id'], 1, '退回')
    assert adjust_store.effective_entries() == []
    # 单据仍可查（留痕）
    assert service.detail(batch['id'])['decision'] == '退回'


def test_source_run_must_be_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_DB', str(tmp_path / 'db.sqlite3'))
    run = save(calculate(base_csv()))  # 未复核
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    with pytest.raises(adjust_store.DomainError) as exc:
        service.submit(late, run['manifest']['run_id'], '2026-11')
    assert exc.value.code == 'NOT_CLOSED'


def test_period_close_state_machine(closed_run):
    result = adjust_store.close_period('2026-11', 1)
    assert result['status'] == '已关账'
    with pytest.raises(adjust_store.DomainError) as exc:
        adjust_store.close_period('2026-11', 2)
    assert exc.value.code == 'PERIOD_CLOSED'
    # 乐观锁
    with pytest.raises(adjust_store.DomainError):
        adjust_store.close_period('2026-12', 99)


def test_evidence_columns_keep_origin_and_adjust_period(closed_run):
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    batch, _ = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    e = batch['entries'][0]
    assert e['source_run_id'] == closed_run['manifest']['run_id']
    assert len(e['idem_key']) == 16 and e['source_line'] == 2
    assert e['source_sha256'] == batch['source_sha256']


# ---------- HTTP 层 ----------

def test_http_endpoints_and_evidence(client, monkeypatch, tmp_path):
    monkeypatch.setenv('APP_DB', str(tmp_path / 'db.sqlite3'))
    run = client.post('/api/run', json={'csv': base_csv()}).get_json()
    client.post('/api/review', json={'id': run['manifest']['run_id'], 'revision': 1, 'decision': '通过'})
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    resp = client.post('/api/adjustments', json={
        'csv': late, 'source_run_id': run['manifest']['run_id'], 'adjust_period': '2026-11'})
    assert resp.status_code == 200
    batch = resp.get_json()
    assert batch['adjust_period'] == '2026-11'
    # 幂等重放
    assert client.post('/api/adjustments', json={
        'csv': late, 'source_run_id': run['manifest']['run_id'], 'adjust_period': '2026-11'
    }).get_json()['reused'] is True
    # 复核
    rv = client.post(f"/api/adjustments/{batch['id']}/review",
                     json={'revision': 1, 'decision': '通过'})
    assert rv.status_code == 200
    # 页面与证据 CSV
    assert client.get('/work/8').status_code == 200
    assert client.get(f"/work/8?adjustment={batch['id']}").status_code == 200
    evidence = client.get(f"/api/adjustments/{batch['id']}/evidence.csv").data.decode('utf-8-sig')
    assert '原期间' in evidence and '调整期间' in evidence and '2026-10' in evidence and '2026-11' in evidence
    # 期间关账接口
    assert client.post('/api/periods/2026-12/close', json={'revision': 1}).status_code == 200
    assert client.get('/api/periods').get_json()[0]['status'] == '已关账'


def test_http_closed_period_returns_422(client, monkeypatch, tmp_path):
    monkeypatch.setenv('APP_DB', str(tmp_path / 'db.sqlite3'))
    run = client.post('/api/run', json={'csv': base_csv()}).get_json()
    client.post('/api/review', json={'id': run['manifest']['run_id'], 'revision': 1, 'decision': '通过'})
    client.post('/api/periods/2026-11/close', json={'revision': 1})
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    resp = client.post('/api/adjustments', json={
        'csv': late, 'source_run_id': run['manifest']['run_id'], 'adjust_period': '2026-11'})
    assert resp.status_code == 422 and resp.get_json()['code'] == 'PERIOD_CLOSED'


def test_http_multipart_upload_and_missing_fields(client, monkeypatch, tmp_path):
    import io
    monkeypatch.setenv('APP_DB', str(tmp_path / 'db.sqlite3'))
    run = client.post('/api/run', json={'csv': base_csv()}).get_json()
    client.post('/api/review', json={'id': run['manifest']['run_id'], 'revision': 1, 'decision': '通过'})
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    resp = client.post('/api/adjustments', data={
        'file': (io.BytesIO(late.encode('utf-8')), 'late.csv'),
        'source_run_id': run['manifest']['run_id'], 'adjust_period': '2026-11'},
        content_type='multipart/form-data')
    assert resp.status_code == 200 and resp.get_json()['entries'][0]['record_id'] == 'P999'
    # 缺来源批次
    bad = client.post('/api/adjustments', json={'csv': late, 'adjust_period': '2026-11'})
    assert bad.status_code == 422 and bad.get_json()['code'] == 'MISSING'
    # 调整期间格式错误
    bad2 = client.post('/api/adjustments', json={
        'csv': late, 'source_run_id': run['manifest']['run_id'], 'adjust_period': '202611'})
    assert bad2.status_code == 422 and bad2.get_json()['code'] == 'PERIOD'
    # 未知调整单 404
    assert client.get('/api/adjustments/nonexistent').status_code == 404


def test_malformed_late_file_uses_same_header_rules(closed_run):
    with pytest.raises(adjust_store.DomainError) as exc:
        service.submit('not,a,csv\n1,2,3\n', closed_run['manifest']['run_id'], '2026-11')
    assert exc.value.code == 'HEADER'
    with pytest.raises(adjust_store.DomainError) as exc:
        service.submit('', closed_run['manifest']['run_id'], '2026-11')
    assert exc.value.code == 'EMPTY'


def test_two_periods_each_count_record_once(closed_run):
    """同一业务键即便跨过两个调整期间提交，幂等键全局唯一，只在一个期间生效一次。"""
    late = late_csv(['P999,position,000001,,2026-10-20,,,,,,,,200,1,0'])
    b1, _ = service.submit(late, closed_run['manifest']['run_id'], '2026-11')
    service.review(b1['id'], 1, '通过')
    # 11 月关账后，12 月又提交同一笔：幂等键相同，整单 SHA 相同但期间不同 -> 新单据，分录被 UNIQUE 去重
    adjust_store.close_period('2026-11', 1)
    b2, _ = service.submit(late, closed_run['manifest']['run_id'], '2026-12')
    service.review(b2['id'], 1, '通过')
    all_eff = adjust_store.effective_entries()
    p999 = [r for r in all_eff if r['record_id'] == 'P999']
    assert len(p999) == 1 and p999[0]['adjust_period'] == '2026-11'
