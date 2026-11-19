from flask import Blueprint, render_template, request
from app.config import TITLE, MODULES, SHORT, SAMPLE
from app.pipeline import calculate
from app.store import latest, save, get_run
from app import adjust_store
bp=Blueprint('web',__name__)
@bp.route('/')
@bp.route('/work/<int:stage>')
def work(stage=1):
    if stage<1 or stage>8:
        from flask import abort
        abort(404)
    if stage==8:
        return adjustment_page()
    run=get_run(request.args['run_id']) if request.args.get('run_id') else latest()
    if run is None: run=save(calculate())
    current=run['stages'][str(stage)]
    return render_template('m'+str(stage)+'.html', title=TITLE, modules=MODULES, short=SHORT, run=run, current=current, stage=stage, sample=SAMPLE.read_text(encoding='utf-8-sig'))

def adjustment_page():
    batch_id=request.args.get('adjustment')
    batch=adjust_store.get_batch(batch_id) if batch_id else None
    batches=adjust_store.list_batches()
    periods=adjust_store.list_periods()
    source=get_run(batch['source_run_id']) if batch else None
    return render_template('m8.html', title=TITLE, modules=MODULES, short=SHORT, stage=8,
                           batch=batch, batches=batches, periods=periods, source=source,
                           run=latest() or save(calculate()),
                           current={'name': '期后调整', 'rows': [], 'metrics': []})
