"""Quiescent-checkpoint WAL teaching model with frozen data-page images.

Scenario knobs (all LSNs are 1-based log positions):

``executed_through``
    Last log record executed before the power cut.
``log_flushed_through``
    Last log record that reached durable storage. Recovery may only use this
    prefix; executed-but-unflushed records are lost.
``page_flushes``
    ``{key: lsn}`` -- after executing that log record the page for ``key`` was
    written back to disk. The page image at that moment is the in-memory value
    of the key, so it may contain uncommitted data (steal / no-force). ``0``
    means the page was never written back.

Write-ahead rule enforced here: a data page (including the all-page write of a
quiescent checkpoint) may only be written back when every log record it
reflects is durable. Every executed checkpoint LSN and every flush LSN must be
``<= log_flushed_through``; otherwise the scenario is rejected instead of
silently producing a history that could not exist on disk.

Recovery starts from the frozen page image (not from the checkpoint), redoes
only durable log actions that the page does not already contain, then undoes
every transaction without a durable commit in reverse LSN order. The result is
compared with an independent winner-only oracle built from the initial ledger.

``crash_after`` remains an alias for ``executed_through`` so old scenarios keep
their original meaning: everything executed is durable and only quiescent
checkpoints write pages back (no-force).
"""
import copy
import hashlib
import json
import re

MODEL_VERSION = 'strict-wal-pageimage-v2'
MAX_RECORDS = 200
MAX_KEYS = 30
MAX_ABS = 1000000000
KEY_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,50}')

REDO = 'redo'
REDO_SKIPPED = 'redo_skipped'
REDO_ABORT = 'redo_abort_undo'
REDO_ABORT_SKIPPED = 'redo_abort_skipped'
UNDO_LOSER = 'undo_loser'
UNDO_NOOP = 'undo_noop'
LOST = 'lost'

SKIP_PAGE = '页已含该动作，不重复重做'
SKIP_LOST = '日志未落盘，崩溃后不可见'
SKIP_NOOP = '页值已是撤销前的值'


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate(initial, records):
    """Validate a complete designed history: keys, locks, checkpoints, ops."""
    if not initial or len(initial) > MAX_KEYS or not records or len(records) > MAX_RECORDS:
        raise ValueError('需1—30个键及1—200条日志')
    for key, value in initial.items():
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key) or not _is_int(value) or abs(value) > MAX_ABS:
            raise ValueError('初始键为字母数字下划线短横线，值为绝对值不超过10亿的整数')
    state = copy.deepcopy(initial)
    active = {}
    used = set()
    locks = {}
    for lsn, record in enumerate(records, 1):
        op = record.get('op')
        tx = record.get('tx')
        if op == 'checkpoint':
            if active:
                raise ValueError(f'LSN {lsn}: 仅支持无活动事务的静止检查点')
            continue
        if not tx:
            raise ValueError(f'LSN {lsn}: 需要事务标识')
        if op == 'begin':
            if tx in used:
                raise ValueError(f'LSN {lsn}: 事务标识不得重复')
            active[tx] = []
            used.add(tx)
        elif op == 'write':
            if tx not in active:
                raise ValueError(f'LSN {lsn}: 写入事务尚未begin')
            key = record.get('key')
            value = record.get('value')
            if key not in initial:
                raise ValueError(f'LSN {lsn}: 写入键必须存在于初始账本')
            if not _is_int(value) or abs(value) > MAX_ABS:
                raise ValueError(f'LSN {lsn}: 数值超限')
            if key in locks and locks[key] != tx:
                raise ValueError(f'LSN {lsn}: 违反严格写锁，活动事务持有同键')
            locks[key] = tx
            active[tx].append((key, state[key]))
            state[key] = value
        elif op in ('commit', 'abort'):
            if tx not in active:
                raise ValueError(f'LSN {lsn}: 事务未开启或已结束')
            if op == 'abort':
                for key, before in reversed(active[tx]):
                    state[key] = before
            del active[tx]
            locks = {k: v for k, v in locks.items() if v != tx}
        else:
            raise ValueError(f'LSN {lsn}: 未知操作')
    return True


def normalize(records, executed_through=None, crash_after=None, log_flushed_through=None, page_flushes=None):
    """Resolve new scenario fields with the legacy ``crash_after`` alias."""
    if executed_through is None:
        executed_through = crash_after
    if executed_through is None:
        raise ValueError('需要执行位置')
    if not _is_int(executed_through) or not 0 <= executed_through <= len(records):
        raise ValueError('执行位置超出日志范围')
    if log_flushed_through is None:
        log_flushed_through = executed_through
    if not _is_int(log_flushed_through) or not 0 <= log_flushed_through <= executed_through:
        raise ValueError('日志落盘位置必须在0到执行位置之间')
    flushes = {}
    for key, lsn in (page_flushes or {}).items():
        if not _is_int(lsn) or not 0 <= lsn <= executed_through:
            raise ValueError(f'键 {key} 的页写回时刻必须在0到执行位置之间')
        flushes[key] = lsn
    return executed_through, log_flushed_through, flushes


def _execute(initial, records, upto, capture=None):
    """Replay ``records[:upto]`` logically and record what each step did."""
    state = copy.deepcopy(initial)
    active = {}
    used = set()
    locks = {}
    winners = []
    aborted = []
    rows = {}
    abort_actions = {}
    snapshots = {}
    checkpoint_snapshots = {}
    checkpoint_lsn = 0
    capture = set(capture or ())
    for lsn, record in enumerate(records[:upto], 1):
        op = record.get('op')
        tx = record.get('tx')
        if op == 'begin':
            active[tx] = []
            used.add(tx)
        elif op == 'write':
            row = {'lsn': lsn, 'tx': tx, 'key': record['key'],
                   'before': state[record['key']], 'after': record['value']}
            active[tx].append(row)
            rows[lsn] = row
            state[record['key']] = record['value']
        elif op == 'abort':
            touched = []
            for row in reversed(active[tx]):
                touched.append(row)
                state[row['key']] = row['before']
            abort_actions[lsn] = touched
            del active[tx]
            aborted.append(tx)
            locks = {k: v for k, v in locks.items() if v != tx}
        elif op == 'commit':
            winners.append(tx)
            del active[tx]
            locks = {k: v for k, v in locks.items() if v != tx}
        elif op == 'checkpoint':
            checkpoint_lsn = lsn
            checkpoint_snapshots[lsn] = copy.deepcopy(state)
        if lsn in capture:
            snapshots[lsn] = copy.deepcopy(state)
    return {'state': state, 'active': active, 'winners': winners,
            'aborted': aborted, 'rows': rows, 'abort_actions': abort_actions,
            'snapshots': snapshots, 'checkpoint_snapshots': checkpoint_snapshots,
            'checkpoint_lsn': checkpoint_lsn}


def _check_flush_keys(initial, page_flushes):
    for key in page_flushes:
        if key not in initial:
            raise ValueError(f'页写回配置的键 {key} 不在初始账本中')


def _check_write_ahead(records, executed_through, log_flushed_through, page_flushes):
    for lsn, record in enumerate(records[:executed_through], 1):
        if record.get('op') == 'checkpoint' and lsn > log_flushed_through:
            raise ValueError(f'LSN {lsn}: 检查点写回全部数据页，但日志仅落盘到 LSN {log_flushed_through}，拒绝越过日志落盘位置')
    for key, lsn in page_flushes.items():
        if lsn > log_flushed_through:
            raise ValueError(f'键 {key} 的页写回 LSN {lsn} 越过日志落盘位置 LSN {log_flushed_through}，拒绝')


def freeze_page_image(initial, records, executed_through=None, crash_after=None,
                      log_flushed_through=None, page_flushes=None):
    """Build the legal on-disk page image for a scenario (called at save time).

    Each key's page is written back at the later of its configured flush LSN
    and the last executed quiescent checkpoint (a checkpoint writes every
    page). The page value is the in-memory value at that LSN, which may include
    uncommitted data; the write-ahead rule guarantees that LSN is durable.

    Returns ``{'values': {...}, 'page_lsn': {...}, 'checkpoint_lsn': n,
    'executed_through': n, 'log_flushed_through': n, 'page_flushes': {...}}``.
    """
    validate(initial, records)
    executed, flushed, flushes = normalize(records, executed_through, crash_after,
                                           log_flushed_through, page_flushes)
    _check_flush_keys(initial, flushes)
    _check_write_ahead(records, executed, flushed, flushes)
    analysis = _execute(initial, records, executed, capture=set(flushes.values()))
    checkpoint = analysis['checkpoint_lsn']
    effective, targets = {}, set()
    for key in initial:
        lsn = max(checkpoint, flushes.get(key, 0))
        effective[key] = lsn
        if lsn:
            targets.add(lsn)
    missing = targets - set(analysis['snapshots']) - set(analysis['checkpoint_snapshots'])
    if missing:
        analysis = _execute(initial, records, executed, capture=set(flushes.values()) | targets)
    values, page_lsn = {}, {}
    for key in initial:
        lsn = effective[key]
        if lsn == 0:
            values[key] = initial[key]
        elif lsn in analysis['checkpoint_snapshots']:
            values[key] = analysis['checkpoint_snapshots'][lsn][key]
        else:
            values[key] = analysis['snapshots'][lsn][key]
        page_lsn[key] = lsn
    return {'values': values, 'page_lsn': page_lsn, 'checkpoint_lsn': checkpoint,
            'executed_through': executed, 'log_flushed_through': flushed,
            'page_flushes': dict(flushes), 'initial': copy.deepcopy(initial)}


def _entry(lsn, tx, phase, key, before, after, reason=None, op=None):
    return {'lsn': lsn, 'tx': tx, 'phase': phase, 'key': key,
            'before': before, 'after': after, 'reason': reason, 'op': op}


def recover(initial, records, crash_after=None, *, executed_through=None,
            log_flushed_through=None, page_flushes=None, page_image=None):
    """Recover from the frozen page image using only the durable log prefix."""
    validate(initial, records)
    executed, flushed, flushes = normalize(records, executed_through, crash_after,
                                           log_flushed_through, page_flushes)
    _check_flush_keys(initial, flushes)
    _check_write_ahead(records, executed, flushed, flushes)
    computed = freeze_page_image(initial, records, executed_through=executed, crash_after=None,
                                 log_flushed_through=flushed, page_flushes=flushes)
    if page_image is not None:
        image = _checked_image(page_image, initial, computed)
    else:
        image = computed
    page_lsn = {key: image['page_lsn'].get(key, 0) for key in initial}
    values = {key: image['values'].get(key, initial[key]) for key in initial}

    prefix = records[:flushed]
    analysis = _execute(initial, prefix, flushed)
    checkpoint_lsn = analysis['checkpoint_lsn']
    durable_checkpoint = (analysis['checkpoint_snapshots'][checkpoint_lsn]
                          if checkpoint_lsn else copy.deepcopy(initial))

    state = copy.deepcopy(values)
    trace = []
    redo_applied = 0
    redo_skipped = 0
    for lsn, record in enumerate(prefix, 1):
        op = record.get('op')
        tx = record.get('tx')
        if op == 'write':
            row = analysis['rows'][lsn]
            key = row['key']
            if page_lsn[key] >= lsn:
                redo_skipped += 1
                trace.append(_entry(lsn, tx, REDO_SKIPPED, key, row['before'], row['after'], SKIP_PAGE, op))
            else:
                state[key] = row['after']
                redo_applied += 1
                trace.append(_entry(lsn, tx, REDO, key, row['before'], row['after'], None, op))
        elif op == 'abort':
            for row in analysis['abort_actions'].get(lsn, []):
                key = row['key']
                if page_lsn[key] >= lsn:
                    redo_skipped += 1
                    trace.append(_entry(lsn, tx, REDO_ABORT_SKIPPED, key, row['after'], row['before'], SKIP_PAGE, op))
                else:
                    state[key] = row['before']
                    trace.append(_entry(lsn, tx, REDO_ABORT, key, row['after'], row['before'], None, op))

    after_redo = copy.deepcopy(state)
    losers = sorted(analysis['active'])
    undo_rows = sorted((row for tx in losers for row in analysis['active'][tx]),
                       key=lambda row: row['lsn'], reverse=True)
    applied_undo = 0
    for row in undo_rows:
        key = row['key']
        if state[key] == row['before']:
            trace.append(_entry(row['lsn'], row['tx'], UNDO_NOOP, key, state[key], row['before'], SKIP_NOOP, 'write'))
        else:
            state[key] = row['before']
            applied_undo += 1
            trace.append(_entry(row['lsn'], row['tx'], UNDO_LOSER, key, row['after'], row['before'], None, 'write'))

    lost = []
    for lsn in range(flushed + 1, executed + 1):
        record = records[lsn - 1]
        row = {'lsn': lsn, 'op': record.get('op'), 'tx': record.get('tx'),
               'key': record.get('key'), 'reason': SKIP_LOST}
        lost.append(row)
        trace.append(_entry(lsn, record.get('tx'), LOST, record.get('key'), None, None, SKIP_LOST, record.get('op')))
    trace.sort(key=lambda item: item['lsn'])
    undo_trace = [item for item in trace if item['phase'] in (UNDO_LOSER, UNDO_NOOP)]
    trace = [item for item in trace if item['phase'] not in (UNDO_LOSER, UNDO_NOOP)] + undo_trace

    oracle = copy.deepcopy(initial)
    for lsn, record in enumerate(prefix, 1):
        if record.get('op') == 'write' and record.get('tx') in analysis['winners']:
            oracle[record['key']] = record['value']

    pages = []
    for key in initial:
        items = [item for item in trace if item['key'] == key]
        skipped = [item for item in items if item['reason'] == SKIP_PAGE]
        pages.append({'key': key, 'initial': initial[key], 'page_lsn': page_lsn[key],
                      'before_recovery': values[key], 'after_recovery': state[key],
                      'redo_applied': len([i for i in items if i['phase'] == REDO]),
                      'redo_skipped': len(skipped),
                      'undo_applied': len([i for i in items if i['phase'] == UNDO_LOSER]),
                      'undo_skipped': len([i for i in items if i['phase'] == UNDO_NOOP]),
                      'skip_reasons': sorted({i['reason'] for i in items if i['reason']})})

    checksum = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    source = json.dumps({'initial': initial, 'records': records,
                         'executed_through': executed, 'log_flushed_through': flushed,
                         'page_flushes': flushes, 'page_image': image},
                        sort_keys=True)
    return {
        'simulation': True, 'model_version': MODEL_VERSION,
        'crash_after': executed, 'executed_through': executed,
        'log_flushed_through': flushed, 'checkpoint_lsn': analysis['checkpoint_lsn'],
        'page_flushes': flushes, 'page_image': values, 'page_lsn': page_lsn,
        'durable_checkpoint': durable_checkpoint, 'after_redo': after_redo,
        'recovered': state, 'oracle': oracle, 'invariant_passed': state == oracle,
        'committed': analysis['winners'], 'aborted': analysis['aborted'], 'losers': losers,
        'lost_records': lost, 'pages': pages,
        'redo_count': redo_applied, 'redo_skipped_count': redo_skipped,
        'undo_count': len(undo_rows), 'undo_applied_count': applied_undo,
        'trace': trace, 'result_sha256': checksum,
        'input_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'boundary': 'Python WAL教学模型；严格写锁、静止检查点、整数键值、页写回受日志落盘位置约束。没有注入真实PostgreSQL崩溃，未执行数据库物理恢复。',
    }


def _checked_image(page_image, initial, computed):
    """Accept the frozen legal image and reject drift from its scenario."""
    if not isinstance(page_image, dict) or 'values' not in page_image:
        raise ValueError('冻结页映像格式无效')
    values = page_image.get('values') or {}
    page_lsn = page_image.get('page_lsn') or {}
    for key in initial:
        if key not in values or not _is_int(values[key]) or abs(values[key]) > MAX_ABS:
            raise ValueError(f'冻结页映像缺少键 {key} 的合法页值')
        if not _is_int(page_lsn.get(key, -1)) or not 0 <= page_lsn.get(key, -1) <= MAX_RECORDS:
            raise ValueError(f'冻结页映像缺少键 {key} 的合法页LSN')
    if values != computed['values'] or page_lsn != computed['page_lsn']:
        raise ValueError('冻结页映像与场景执行位置/日志落盘位置/页写回时刻不一致')
    return {'values': values, 'page_lsn': page_lsn,
            'checkpoint_lsn': computed['checkpoint_lsn']}
