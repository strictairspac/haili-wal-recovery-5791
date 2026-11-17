from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import Recovery, CrashCase, LogSet
from src.services.wal_engine import freeze_page_image
from src.utils.serialize import record
from src.services.crash_case_service import case_parameters


def list_recoveries(db):
    return [record(x) for x in db.scalars(select(Recovery).order_by(Recovery.id.desc()).limit(100))]


def get_recovery(db, ident):
    obj = db.get(Recovery, ident)
    if obj is None:
        raise HTTPException(404, '恢复记录不存在')
    return obj


def snapshot_scenario(snapshot):
    """Resolve a frozen snapshot into ``recover`` keyword arguments.

    Snapshots written before the page-image scenario existed only carry
    ``case.crash_after``; snapshots written now also carry ``parameters`` and
    ``page_image``. Both resolve here so the worker never reads mutable rows.
    """
    snapshot = snapshot or {}
    params = snapshot.get('parameters') or {}
    case = snapshot.get('case') or {}

    def pick(name):
        value = params.get(name, case.get(name))
        return value if value is not None else None

    legacy = params.get('crash_after', case.get('crash_after'))
    image = snapshot.get('page_image') or case.get('page_image')
    if params.get('legacy'):
        # Old scenario: crash point only, no-force pages, but still recover from
        # the frozen image when the queue snapshot carries one.
        return {'crash_after': legacy, 'page_image': image} if image else {'crash_after': legacy}
    executed = pick('executed_through')
    flushed = pick('log_flushed_through')
    flushes = pick('page_flushes') or {}
    if executed is None and flushed is None and image is None and flushes == {}:
        # Legacy snapshot: only the crash point, and only checkpoints write pages.
        return {'crash_after': legacy}
    return {'crash_after': legacy,
            'executed_through': executed if executed is not None else legacy,
            'log_flushed_through': flushed if flushed is not None else legacy,
            'page_flushes': flushes, 'page_image': image}


def create_recovery(db, data):
    case = db.get(CrashCase, data.crash_case_id)
    if case is None:
        raise HTTPException(404, '故障场景不存在')
    if len(db.scalars(select(Recovery.id).where(Recovery.status.in_(['queued', 'running']))).all()) >= 20:
        raise HTTPException(429, '待处理配额20已满')
    logs = db.get(LogSet, case.log_set_id)
    params = case_parameters(case)
    executed = params.get('executed_through', params.get('crash_after'))
    flushed = params.get('log_flushed_through', executed)
    flushes = params.get('page_flushes') or {}
    # The frozen image is stored with the case at save time; older cases may
    # not have one, so derive and freeze the legal image now. For legacy rows
    # this is the no-force checkpoint image, identical to their old semantics.
    image = case.page_image
    if image is None:
        try:
            image = freeze_page_image(logs.initial, logs.records, executed_through=executed,
                                      log_flushed_through=flushed, page_flushes=flushes)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        case.page_image = image
    snapshot = {'log_set': record(logs), 'case': record(case),
                'page_image': image, 'parameters': params}
    obj = Recovery(**data.model_dump(), status='queued', snapshot=snapshot)
    db.add(obj)
    db.flush()
    return obj
