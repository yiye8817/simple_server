"""Versioned weekly rules and dated snapshots for paired Android devices.

All schedule writes, rolling materialization and page reads are serialized in a
SQLite transaction. A changed revision between pages rejects continuation; the
client stages pages and keeps its last complete durable cache on that conflict.
"""
import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, g, jsonify, render_template, request

from .android import integer, managed_device, object_body, require_android, text, validated
from .db import get_db
from .security import require_user

bp = Blueprint('agenda', __name__)
PAGE_SIZE = 200
RULE_LIMIT = 128
CACHE_LIMIT = 16 * 1024 * 1024
OCCURRENCE_FIELDS = ('id', 'rule_id', 'local_date', 'timezone', 'start_ms', 'end_ms', 'title', 'notes', 'status', 'version', 'completed_ms')
RULE_FIELDS = ('id', 'title', 'notes', 'start_minute', 'end_minute', 'valid_from', 'valid_until', 'version')


def now_ms():
    return int(time.time() * 1000)


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('日期须为 YYYY-MM-DD')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError('日期无效') from None


def rule_json(row):
    return {**{key: row[key] for key in RULE_FIELDS}, 'weekdays': json.loads(row['weekdays_json']), 'active': bool(row['active'])}


def occurrence_json(row):
    return {key: row[key] for key in OCCURRENCE_FIELDS}


def parse_rule(body, today):
    title = text(body.get('title'), '课程名称', 80).strip()
    notes = body.get('notes', '')
    # Notes can contain line breaks, but must remain bounded plain text.
    if not isinstance(notes, str) or len(notes) > 500 or any(ord(c) < 32 and c not in '\n\r\t' for c in notes):
        raise ValueError('备注无效')
    days = body.get('weekdays')
    if not isinstance(days, list) or not days or any(type(d) is not int or not 1 <= d <= 7 for d in days) or days != sorted(set(days)):
        raise ValueError('weekdays 须为排序且不重复的 1..7')
    start = integer(body.get('start_minute'), 'start_minute', 0, 1438)
    end = integer(body.get('end_minute'), 'end_minute', 1, 1439)
    if end <= start:
        raise ValueError('结束时间须晚于开始时间，不支持跨夜')
    first = iso_date(body['valid_from']) if body.get('valid_from') is not None else today
    last = iso_date(body['valid_until']) if body.get('valid_until') is not None else None
    if last is not None and last < first:
        raise ValueError('学期结束日期不能早于开始日期')
    return dict(title=title, notes=notes, weekdays_json=json.dumps(days), start_minute=start, end_minute=end,
                valid_from=first.isoformat(), valid_until=last.isoformat() if last else None)


def enqueue_sync(db, device_id):
    android = db.execute('SELECT capabilities_json FROM android_devices WHERE device_id=?', (device_id,)).fetchone()
    if json.loads(android['capabilities_json']).get('weekly_schedule') is not True:
        return
    # A claimed task can finish just before a mutation. Keep at most one pending
    # successor so an in-flight sync cannot consume a later notification.
    if not db.execute("SELECT 1 FROM tasks WHERE device_id=? AND kind='android.schedule.sync' AND status='pending'", (device_id,)).fetchone():
        user = getattr(g, 'user', None)
        db.execute('INSERT INTO tasks(device_id,kind,payload_json,status,created_by,created_at) VALUES(?,?,?,?,?,?)',
                   (device_id, 'android.schedule.sync', '{}', 'pending', user['id'] if user else None, int(time.time())))


def bump(db, device_id):
    db.execute('UPDATE android_schedule_state SET revision=revision+1 WHERE device_id=?', (device_id,))
    enqueue_sync(db, device_id)


def local_instant(day, minute, zone):
    naive = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
    aware = naive.replace(tzinfo=zone, fold=0)
    # Roundtrip rejects a nonexistent wall clock time during spring DST gaps.
    if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return int(aware.timestamp() * 1000)


def eligible(rule, day):
    return rule['active'] and day.isoweekday() in json.loads(rule['weekdays_json']) and rule['valid_from'] <= day.isoformat() and (rule['valid_until'] is None or day.isoformat() <= rule['valid_until'])


def materialize(db, device_id, now):
    zone_name = db.execute('SELECT timezone FROM android_devices WHERE device_id=?', (device_id,)).fetchone()['timezone']
    zone = ZoneInfo(zone_name)
    today = datetime.fromtimestamp(now / 1000, zone).date()
    through = today + timedelta(days=55)
    week = today - timedelta(days=today.weekday())
    db.execute('INSERT OR IGNORE INTO android_schedule_state(device_id,timezone) VALUES(?,?)', (device_id, zone_name))
    state = db.execute('SELECT * FROM android_schedule_state WHERE device_id=?', (device_id,)).fetchone()
    changed = (state['timezone'], state['through_date'], state['week_start']) != (zone_name, through.isoformat(), week.isoformat())
    rules = db.execute('SELECT * FROM android_schedule_rules WHERE device_id=?', (device_id,)).fetchall()
    for rule in rules:
        existing = {row['local_date']: row for row in db.execute('SELECT * FROM android_schedule_occurrences WHERE rule_id=?', (rule['id'],))}
        # Update every unstarted pending snapshot, including rows beyond a newly
        # shortened term. Started/completed/cancelled snapshots are immutable here.
        for key, row in existing.items():
            if row['status'] != 'pending' or row['start_ms'] <= now or row['manual_override']:
                continue
            day = date.fromisoformat(key)
            start = local_instant(day, rule['start_minute'], zone)
            end = local_instant(day, rule['end_minute'], zone)
            valid = eligible(rule, day) and start is not None and end is not None and end > start and start > now
            values = (start, end, rule['title'], rule['notes'], 'pending', zone_name) if valid else (row['start_ms'], row['end_ms'], row['title'], row['notes'], 'cancelled', row['timezone'])
            if values != tuple(row[k] for k in ('start_ms','end_ms','title','notes','status','timezone')):
                db.execute('UPDATE android_schedule_occurrences SET start_ms=?,end_ms=?,title=?,notes=?,status=?,timezone=?,version=version+1 WHERE id=?', (*values, row['id']))
                changed = True
        if not rule['active']:
            continue
        assigned_day = datetime.fromtimestamp(rule['created_ms'] / 1000, zone).date()
        first = max(assigned_day, date.fromisoformat(rule['valid_from']))
        # Avoid a multi-year scan on each sync: after the first materialization,
        # only extend the previous window. Rule edits rescan from today below.
        if state['timezone'] == zone_name and state['through_date'] and rule['materialized_version'] == rule['version']:
            first = max(first, date.fromisoformat(state['through_date']) + timedelta(days=1))
        elif rule['version'] > 1:
            first = max(first, today)
        last = min(through, date.fromisoformat(rule['valid_until'])) if rule['valid_until'] else through
        day = first
        while day <= last:
            if day.isoformat() not in existing and eligible(rule, day):
                start, end = local_instant(day, rule['start_minute'], zone), local_instant(day, rule['end_minute'], zone)
                if start is not None and end is not None and end > start and end > rule['created_ms'] and (rule['version'] == 1 or start > rule['changed_ms']):
                    db.execute('INSERT INTO android_schedule_occurrences(device_id,rule_id,local_date,timezone,start_ms,end_ms,title,notes) VALUES(?,?,?,?,?,?,?,?)',
                               (device_id, rule['id'], day.isoformat(), zone_name, start, end, rule['title'], rule['notes']))
                    changed = True
            day += timedelta(days=1)
        db.execute('UPDATE android_schedule_rules SET materialized_version=version WHERE id=?', (rule['id'],))
    db.execute('UPDATE android_schedule_state SET timezone=?,through_date=?,week_start=? WHERE device_id=?', (zone_name, through.isoformat(), week.isoformat(), device_id))
    if changed:
        bump(db, device_id)
    return db.execute('SELECT * FROM android_schedule_state WHERE device_id=?', (device_id,)).fetchone()


@contextmanager
def schedule_transaction(device_id):
    db = get_db()
    db.execute('BEGIN IMMEDIATE')
    try:
        now = now_ms()
        state = materialize(db, device_id, now)
        yield db, state, now
        db.commit()
    except Exception:
        db.rollback()
        raise


def owned(device_id):
    _, error = managed_device(device_id)
    return error


@bp.get('/android/schedule/')
def page():
    return render_template('agenda.html')


@bp.get('/api/android/devices/<int:device_id>/schedule')
@require_user()
def management_state(device_id):
    error = owned(device_id)
    if error:
        return error
    with schedule_transaction(device_id) as (db, state, _):
        rules = [rule_json(row) for row in db.execute('SELECT * FROM android_schedule_rules WHERE device_id=? AND active=1 ORDER BY id', (device_id,))]
        result = {k: state[k] for k in ('revision','ack_revision','ack_at','timezone','through_date')}
        return jsonify(**result, rules=rules)


@bp.post('/api/android/devices/<int:device_id>/schedule/rules')
@require_user()
@validated
def create_rule(device_id):
    error = owned(device_id)
    if error:
        return error
    return create_schedule_rule(device_id)


def create_schedule_rule(device_id):
    """One transactional write path for owned management and paired agents."""
    body = object_body()
    with schedule_transaction(device_id) as (db, state, now):
        today = datetime.fromtimestamp(now / 1000, ZoneInfo(state['timezone'])).date()
        values = parse_rule(body, today)
        clause = ' AND '.join(key + ' IS ?' for key in values)
        existing = db.execute('SELECT * FROM android_schedule_rules WHERE device_id=? AND active=1 AND '+clause, (device_id, *values.values())).fetchone()
        if existing:
            return jsonify(rule=rule_json(existing), revision=state['revision'])
        if db.execute('SELECT count(*) FROM android_schedule_rules WHERE device_id=? AND active=1', (device_id,)).fetchone()[0] >= RULE_LIMIT:
            return jsonify(error='每台设备至多 128 条有效课程规则'), 413
        keys = ','.join(values)
        placeholders = ','.join('?' for _ in values)
        cursor = db.execute(f'INSERT INTO android_schedule_rules(device_id,{keys},created_ms,changed_ms) VALUES(?,{placeholders},?,?)', (device_id, *values.values(), now, now))
        bump(db, device_id)
        state = materialize(db, device_id, now)
        row = db.execute('SELECT * FROM android_schedule_rules WHERE id=?', (cursor.lastrowid,)).fetchone()
        return jsonify(rule=rule_json(row), revision=state['revision'])


@bp.route('/api/android/devices/<int:device_id>/schedule/rules/<int:rule_id>', methods=['PUT','DELETE'])
@require_user()
@validated
def edit_rule(device_id, rule_id):
    error = owned(device_id)
    if error:
        return error
    return edit_schedule_rule(device_id, rule_id)


def edit_schedule_rule(device_id, rule_id, *, agent=False):
    body = object_body()
    expected = integer(body.get('expected_version'), 'expected_version', 1)
    with schedule_transaction(device_id) as (db, state, now):
        row = db.execute('SELECT * FROM android_schedule_rules WHERE device_id=? AND id=?', (device_id, rule_id)).fetchone()
        if row is None:
            return jsonify(error='课程不存在'), 404
        if row['version'] != expected or not row['active']:
            return jsonify(error='课程已被修改，请刷新后重试', rule=rule_json(row)), 409
        if request.method == 'DELETE':
            db.execute('UPDATE android_schedule_rules SET active=0,version=version+1,changed_ms=? WHERE id=?', (now, rule_id))
        else:
            today = datetime.fromtimestamp(now / 1000, ZoneInfo(state['timezone'])).date()
            values = parse_rule(body, today)
            db.execute('UPDATE android_schedule_rules SET '+','.join(k+'=?' for k in values)+',version=version+1,changed_ms=? WHERE id=?', (*values.values(), now, rule_id))
        # A later explicit plan edit supersedes single-instance overrides only
        # for pending work that has not started. Historical snapshots stay put.
        db.execute("UPDATE android_schedule_occurrences SET manual_override=0 WHERE rule_id=? AND status='pending' AND start_ms>?", (rule_id, now))
        bump(db, device_id)
        state = materialize(db, device_id, now)
        row = db.execute('SELECT * FROM android_schedule_rules WHERE id=?', (rule_id,)).fetchone()
        if agent and request.method == 'DELETE':
            return jsonify(ok=True, revision=state['revision'])
        return jsonify(rule=rule_json(row), revision=state['revision'])


@bp.route('/api/android/agent/schedule/rules', methods=['GET', 'POST'])
@require_android
@validated
def agent_rules():
    # Never resolve the target from a supplied device_id or owner_id. Device
    # authentication and the Android pairing above establish the only scope.
    device_id = g.device['id']
    if request.method == 'POST':
        return create_schedule_rule(device_id)
    with schedule_transaction(device_id) as (db, state, _):
        rules = [rule_json(row) for row in db.execute(
            'SELECT * FROM android_schedule_rules WHERE device_id=? AND active=1 ORDER BY id', (device_id,))]
        return jsonify(rules=rules, revision=state['revision'])


@bp.route('/api/android/agent/schedule/rules/<int:rule_id>', methods=['PUT', 'DELETE'])
@require_android
@validated
def agent_edit_rule(rule_id):
    return edit_schedule_rule(g.device['id'], rule_id, agent=True)


def query_integer(key, default=0, maximum=2**63-1):
    value = request.args.get(key)
    if value is None:
        return default
    if not re.fullmatch(r'\d{1,19}', value):
        raise ValueError(key+' 无效')
    return integer(int(value), key, 0, maximum)


@bp.get('/api/android/devices/<int:device_id>/schedule/occurrences')
@require_user()
@validated
def management_occurrences(device_id):
    error = owned(device_id)
    if error:
        return error
    overdue = request.args.get('overdue') == '1'
    day = iso_date(request.args.get('date')).isoformat() if not overdue else None
    after = query_integer('after_id')
    limit = query_integer('limit', PAGE_SIZE, PAGE_SIZE)
    if limit < 1:
        raise ValueError('limit 须为 1..200')
    with schedule_transaction(device_id) as (db, state, now):
        clause = 'device_id=? AND '+("status='pending' AND end_ms<=?" if overdue else 'local_date=?')
        args = (device_id, now if overdue else day)
        total = db.execute('SELECT count(*) FROM android_schedule_occurrences WHERE '+clause, args).fetchone()[0]
        continuation, continuation_args = '', ()
        if after:
            cursor = db.execute('SELECT * FROM android_schedule_occurrences WHERE device_id=? AND id=?', (device_id, after)).fetchone()
            if cursor is None:
                raise ValueError('分页游标无效')
            order_key = 'end_ms' if overdue else 'start_ms'
            continuation = f' AND ({order_key}>? OR ({order_key}=? AND id>?))'
            continuation_args = (cursor[order_key], cursor[order_key], after)
        rows = db.execute('SELECT * FROM android_schedule_occurrences WHERE '+clause+continuation+' ORDER BY '+('end_ms' if overdue else 'start_ms')+',id LIMIT ?', (*args,*continuation_args,limit+1)).fetchall()
        return jsonify(occurrences=[occurrence_json(r) for r in rows[:limit]], total_count=total, next_after=rows[limit-1]['id'] if len(rows)>limit else None, revision=state['revision'])


def set_status(db, row, status, now, *, manual=False):
    override = 1 if manual and status == 'pending' else 0 if manual and status == 'cancelled' else row['manual_override']
    if row['status'] != status or override != row['manual_override']:
        db.execute('UPDATE android_schedule_occurrences SET status=?,completed_ms=?,manual_override=?,version=version+1 WHERE id=?', (status, now if status == 'completed' else None, override, row['id']))
        bump(db, row['device_id'])
    return db.execute('SELECT * FROM android_schedule_occurrences WHERE id=?', (row['id'],)).fetchone()


@bp.put('/api/android/devices/<int:device_id>/schedule/occurrences/<int:occurrence_id>')
@require_user()
@validated
def edit_occurrence(device_id, occurrence_id):
    error = owned(device_id)
    if error:
        return error
    body = object_body()
    expected = integer(body.get('expected_version'), 'expected_version')
    status = body.get('status')
    if status not in ('pending','completed','cancelled'):
        raise ValueError('status 无效')
    with schedule_transaction(device_id) as (db, _, now):
        row = db.execute('SELECT * FROM android_schedule_occurrences WHERE device_id=? AND id=?', (device_id, occurrence_id)).fetchone()
        if row is None:
            return jsonify(error='日程不存在'), 404
        if row['version'] != expected:
            return jsonify(error='日程已被修改，请刷新', occurrence=occurrence_json(row)), 409
        row = set_status(db, row, status, now, manual=True)
        revision = db.execute('SELECT revision FROM android_schedule_state WHERE device_id=?', (device_id,)).fetchone()[0]
        return jsonify(occurrence=occurrence_json(row), revision=revision)


@bp.get('/api/android/agent/schedule')
@require_android
@validated
def agent_schedule():
    device_id = g.device['id']
    after = query_integer('after_id')
    known = query_integer('known_revision', None)
    if after and known is None:
        raise ValueError('后续分页必须包含 known_revision')
    with schedule_transaction(device_id) as (db, state, now):
        metadata = {key: state[key] for key in ('revision','timezone','week_start','through_date')}
        if after and known != state['revision']:
            return jsonify(error='日程已改变，请从第一页重新同步', revision=state['revision']), 409
        clause = "device_id=? AND ((local_date>=? AND local_date<=?) OR (status='pending' AND local_date<?))"
        args = (device_id,state['week_start'],state['through_date'],state['week_start'])
        # Read revision and rows within this single transaction, then paginate.
        rows = db.execute('SELECT * FROM android_schedule_occurrences WHERE '+clause+' ORDER BY id', args).fetchall()
        total = len(rows)
        if total > 10000:
            return jsonify(error='日程超过 10000 项，请在服务器整理已逾期任务', limit=10000), 413
        payload = [occurrence_json(r) for r in rows]
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > CACHE_LIMIT:
            return jsonify(error='日程缓存超过 16 MiB 限制', limit=CACHE_LIMIT), 413
        unchanged = not after and known == state['revision']
        page = [] if unchanged else [r for r in payload if r['id'] > after][:PAGE_SIZE+1]
        more = len(page) > PAGE_SIZE
        return jsonify(**metadata, generated_ms=now, total_count=total, unchanged=unchanged,
                       occurrences=page[:PAGE_SIZE], next_after=page[PAGE_SIZE-1]['id'] if more else None)


@bp.post('/api/android/agent/schedule/ack')
@require_android
@validated
def agent_ack():
    revision = integer(object_body().get('revision'), 'revision')
    device_id = g.device['id']
    with schedule_transaction(device_id) as (db, state, now):
        if revision > state['revision']:
            return jsonify(error='不能确认尚不存在的版本', revision=state['revision']), 409
        if state['ack_revision'] is None or revision >= state['ack_revision']:
            db.execute('UPDATE android_schedule_state SET ack_revision=?,ack_at=? WHERE device_id=?', (revision, now//1000, device_id))
        return jsonify(ok=True, revision=state['revision'])


def undo_would_be_cancelled(db, row, now):
    """Never report a successful undo which the next sync would recancel."""
    if row['manual_override'] or row['start_ms'] <= now:
        return False
    rule = db.execute('SELECT * FROM android_schedule_rules WHERE id=?', (row['rule_id'],)).fetchone()
    day = date.fromisoformat(row['local_date'])
    if not eligible(rule, day):
        return True
    zone_name = db.execute('SELECT timezone FROM android_devices WHERE device_id=?', (row['device_id'],)).fetchone()[0]
    zone = ZoneInfo(zone_name)
    start, end = local_instant(day, rule['start_minute'], zone), local_instant(day, rule['end_minute'], zone)
    return start is None or end is None or end <= start or start <= now


@bp.post('/api/android/agent/schedule/completions')
@require_android
@validated
def agent_completion():
    body = object_body()
    operation = body.get('operation_id')
    try:
        if str(uuid.UUID(operation)) != operation:
            raise ValueError('operation_id 须为规范 UUID')
    except (ValueError, TypeError, AttributeError):
        raise ValueError('operation_id 须为规范 UUID') from None
    occurrence_id = integer(body.get('occurrence_id'), 'occurrence_id', 1)
    expected = integer(body.get('expected_version'), 'expected_version')
    completed = body.get('completed')
    if type(completed) is not bool:
        raise ValueError('completed 须为布尔值')
    device_id = g.device['id']
    fingerprint = json.dumps([occurrence_id, expected, completed])
    with schedule_transaction(device_id) as (db, state, now):
        row = db.execute('SELECT * FROM android_schedule_occurrences WHERE device_id=? AND id=?', (device_id,occurrence_id)).fetchone()
        if row is None:
            return jsonify(error='日程不存在'), 404
        previous = db.execute('SELECT * FROM android_schedule_operations WHERE device_id=? AND operation_id=?', (device_id,operation)).fetchone()
        if previous:
            if previous['payload_json'] != fingerprint:
                return jsonify(error='operation_id 已用于不同请求', occurrence=occurrence_json(row)), 409
            if not previous['applied']:
                return jsonify(error='日程已由服务器修改，请刷新后重试', occurrence=occurrence_json(row), revision=state['revision']), 409
            return jsonify(operation_id=operation, occurrence=occurrence_json(row), revision=state['revision'], duplicate=True)
        if row['version'] != expected or row['status'] == 'cancelled' or (not completed and undo_would_be_cancelled(db, row, now)):
            db.execute('INSERT INTO android_schedule_operations(device_id,operation_id,payload_json,created_ms,applied) VALUES(?,?,?,?,0)', (device_id,operation,fingerprint,now))
            return jsonify(error='日程已由服务器修改，请刷新后重试', occurrence=occurrence_json(row), revision=state['revision']), 409
        row = set_status(db, row, 'completed' if completed else 'pending', now)
        db.execute('INSERT INTO android_schedule_operations(device_id,operation_id,payload_json,created_ms) VALUES(?,?,?,?)', (device_id,operation,fingerprint,now))
        revision = db.execute('SELECT revision FROM android_schedule_state WHERE device_id=?', (device_id,)).fetchone()[0]
        return jsonify(operation_id=operation, occurrence=occurrence_json(row), revision=revision, duplicate=False)
