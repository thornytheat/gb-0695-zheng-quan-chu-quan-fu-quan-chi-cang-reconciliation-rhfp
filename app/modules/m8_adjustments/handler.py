from flask import Blueprint, request, jsonify

from app.errors import DomainError
from app.modules.m8_adjustments import service
from app import adjust_store

bp = Blueprint('m8', __name__)


@bp.route('/api/adjustments', methods=['GET', 'POST'])
def adjustments():
    if request.method == 'GET':
        return jsonify(adjust_store.list_batches())
    if request.files:
        if 'file' not in request.files:
            raise DomainError('REQUEST', '上传必须使用file字段', 400)
        raw = request.files['file'].read()
        result, _ = service.submit(
            raw_bytes=raw,
            source_run_id=request.form.get('source_run_id'),
            adjust_period=request.form.get('adjust_period'),
        )
        return jsonify(result)
    payload = request.get_json(silent=True) or {}
    result, _ = service.submit(
        late_csv=payload.get('csv'),
        source_run_id=payload.get('source_run_id'),
        adjust_period=payload.get('adjust_period'),
    )
    return jsonify(result)


@bp.route('/api/adjustments/<batch_id>')
def adjustment_detail(batch_id):
    return jsonify(service.detail(batch_id))


@bp.route('/api/adjustments/<batch_id>/review', methods=['POST'])
def adjustment_review(batch_id):
    body = request.get_json(silent=True) or {}
    try:
        revision = int(body.get('revision', 0))
    except (ValueError, TypeError):
        raise DomainError('VERSION', '版本必须为整数')
    return jsonify(service.review(batch_id, revision, body.get('decision')))


@bp.route('/api/periods', methods=['GET'])
def periods():
    return jsonify(adjust_store.list_periods())


@bp.route('/api/periods/<period>/close', methods=['POST'])
def close(period):
    body = request.get_json(silent=True) or {}
    try:
        revision = int(body.get('revision', 1))
    except (ValueError, TypeError):
        raise DomainError('VERSION', '版本必须为整数')
    return jsonify(adjust_store.close_period(period, revision))
