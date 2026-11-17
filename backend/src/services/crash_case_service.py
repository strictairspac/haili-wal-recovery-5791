from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import CrashCase, LogSet
from src.services.wal_engine import freeze_page_image
from src.utils.serialize import record


def list_crash_cases(db):
    return [record(x) for x in db.scalars(select(CrashCase).order_by(CrashCase.id))]


def case_parameters(case):
    """Scenario knobs with the legacy crash_after fallback.

    A truthful ``legacy`` flag lets the worker apply the old no-force rule
    (checkpoints only) instead of treating an old row as an explicit
    "everything durable, no page ever written back" scenario.
    """
    if case.executed_through is None:
        return {'legacy': True, 'crash_after': case.crash_after}
    return {'legacy': False, 'executed_through': case.executed_through,
            'crash_after': case.executed_through,
            'log_flushed_through': case.log_flushed_through,
            'page_flushes': case.page_flushes or {}}


def create_crash_case(db, data):
    logs = db.get(LogSet, data.log_set_id)
    if logs is None:
        raise HTTPException(404, '日志集不存在')
    executed = data.executed_through if data.executed_through is not None else data.crash_after
    if executed > len(logs.records):
        raise HTTPException(422, '执行位置超过日志条数')
    flushed = data.log_flushed_through if data.log_flushed_through is not None else executed
    if flushed > executed:
        raise HTTPException(422, '日志落盘位置不能超过执行位置')
    flushes = data.page_flushes or {}
    try:
        image = freeze_page_image(logs.initial, logs.records, executed_through=executed,
                                  log_flushed_through=flushed, page_flushes=flushes)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    obj = CrashCase(name=data.name, log_set_id=data.log_set_id, crash_after=executed,
                    executed_through=executed, log_flushed_through=flushed,
                    page_flushes=flushes, page_image=image)
    db.add(obj)
    db.flush()
    return record(obj)
