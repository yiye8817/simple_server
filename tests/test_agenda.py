"""Weekly agenda contract, isolated SQLite and real Flask routes."""
import json
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from unittest.mock import patch

from dev_control import create_app
from dev_control.db import get_db


def ms(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


class AgendaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'ENABLE_LEGACY_TOOLS': False,
                               'DATA_DIR': self.tmp.name + '/data', 'SERVER_FILES_ROOT': self.tmp.name + '/files',
                               'ADMIN_PASSWORD': 'isolated-test-password'})
        self.c = self.app.test_client()
        self.admin = self.login('admin')
        for name in ('alice', 'other'):
            self.c.post('/api/auth/users', headers=self.admin, json={'username': name, 'password': 'isolated-test-password'})
        self.owner, self.other = self.login('alice'), self.login('other')
        self.dev, self.agent = self.pair('one')
        self.base = f'/api/android/devices/{self.dev}/schedule'
        self.clock = patch('dev_control.agenda.now_ms', return_value=ms('2026-10-25T04:00:00'))
        try:
            self.clock.start()
        except (ModuleNotFoundError, AttributeError):
            # Missing blueprint must be observed as an HTTP contract failure first.
            self.clock = None

    def tearDown(self):
        if self.clock:
            self.clock.stop()
        self.tmp.cleanup()

    def login(self, name):
        return {'Authorization': 'Bearer ' + self.c.post('/api/auth/login', json={'username': name, 'password': 'isolated-test-password'}).json['token']}

    def pair(self, uid, owner=None, zone='Asia/Shanghai', capable=True):
        code = self.c.post('/api/android/pairing-codes', headers=owner or self.owner).json['code']
        r = self.c.post('/api/android/pair', json={'code': code, 'device_uid': uid, 'name': uid, 'capabilities': {'weekly_schedule': capable}})
        dev = r.json['device']['id']
        agent = {'X-Device-Token': r.json['device']['device_token']}
        self.c.post('/api/android/agent/inventory', headers=agent, json={'name': uid, 'timezone': zone, 'capabilities': {'weekly_schedule': capable}, 'apps': []})
        return dev, agent

    def body(self, **changes):
        data = dict(title='数学 <script>', notes='第一章', weekdays=[7], start_minute=12*60, end_minute=13*60,
                    valid_from=None, valid_until=None)
        data.update(changes)
        return data

    def create(self, **changes):
        r = self.c.post(self.base + '/rules', headers=self.owner, json=self.body(**changes))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.json['rule']

    def snapshot(self, **query):
        return self.c.get('/api/android/agent/schedule', headers=self.agent, query_string=query)

    def day(self, day='2026-10-25'):
        r = self.c.get(self.base + '/occurrences', headers=self.owner, query_string={'date': day})
        self.assertEqual(r.status_code, 200)
        return r.json['occurrences']

    def complete(self, row, **changes):
        body = dict(operation_id=str(uuid.uuid4()), occurrence_id=row['id'], expected_version=row['version'], completed=True)
        body.update(changes)
        return self.c.post('/api/android/agent/schedule/completions', headers=self.agent, json=body), body

    def test_routes_auth_owner_and_agent_isolation(self):
        self.assertEqual(self.c.get(self.base).status_code, 401)
        self.assertEqual(self.c.get(self.base, headers=self.other).status_code, 403)
        self.assertEqual(self.c.get('/api/android/agent/schedule').status_code, 401)
        self.assertEqual(self.c.get(self.base, headers=self.owner).status_code, 200)
        self.assertEqual(self.c.get('/android/schedule/').status_code, 200)
        rule = self.create()
        foreign, foreign_agent = self.pair('foreign', self.other)
        for method, path, body in [('put', f'/rules/{rule["id"]}', self.body(expected_version=1)), ('delete', f'/rules/{rule["id"]}', {'expected_version': 1})]:
            self.assertEqual(getattr(self.c, method)(f'/api/android/devices/{foreign}/schedule'+path, headers=self.other, json=body).status_code, 404)
        occurrence = self.day()[0]
        self.assertEqual(self.c.post('/api/android/agent/schedule/completions', headers=foreign_agent, json={'operation_id': str(uuid.uuid4()), 'occurrence_id': occurrence['id'], 'expected_version': 0, 'completed': True}).status_code, 404)

    def test_agent_rule_crud_is_device_scoped_and_ignores_redirect_fields(self):
        route = '/api/android/agent/schedule/rules'
        foreign, foreign_agent = self.pair('foreign', self.other)
        foreign_rule = self.c.post(f'/api/android/devices/{foreign}/schedule/rules', headers=self.other,
                                  json=self.body(title='other device')).json['rule']
        created = self.c.post(route, headers=self.agent, json=self.body(device_id=foreign, owner_id=999))
        self.assertEqual(created.status_code, 200)
        self.assertEqual(set(created.json), {'rule', 'revision'})
        rule = created.json['rule']
        own = self.c.get(route, headers=self.agent)
        self.assertEqual(set(own.json), {'rules', 'revision'})
        self.assertEqual([r['id'] for r in own.json['rules']], [rule['id']])
        self.assertEqual([r['id'] for r in self.c.get(route, headers=foreign_agent).json['rules']], [foreign_rule['id']])
        for method in ('put', 'delete'):
            response = getattr(self.c, method)(route+'/'+str(foreign_rule['id']), headers=self.agent,
                                               json=self.body(expected_version=1, device_id=foreign))
            self.assertEqual(response.status_code, 404)
        removed = self.c.delete(route+'/'+str(rule['id']), headers=self.agent, json={'expected_version': 1})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(set(removed.json), {'ok', 'revision'})
        self.assertTrue(removed.json['ok'])
        self.assertEqual(self.c.get(route, headers=self.agent).json['rules'], [])
        self.assertEqual(self.c.get(route, headers=foreign_agent).json['rules'], [foreign_rule])

    def test_agent_rules_require_paired_device_credentials_for_every_method(self):
        route = '/api/android/agent/schedule/rules'
        for headers in ({}, {'X-Device-Token': 'invalid'}, self.owner):
            for method, suffix in [('get', ''), ('post', ''), ('put', '/1'), ('delete', '/1')]:
                with self.subTest(headers=list(headers), method=method):
                    response = getattr(self.c, method)(route+suffix, headers=headers, json=self.body(expected_version=1))
                    self.assertEqual(response.status_code, 401)
        from dev_control.security import hash_token
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO devices(device_uid,name,token_hash,created_at) VALUES('generic','generic',?,0)", (hash_token('generic-token'),))
            db.commit()
        self.assertEqual(self.c.get(route, headers={'X-Device-Token': 'generic-token'}).status_code, 403)

    def test_agent_edits_share_versions_validation_and_immutable_history(self):
        route = '/api/android/agent/schedule/rules'
        rule = self.create(weekdays=[1,7], valid_until='2026-12-31')
        started = self.day()[0]
        future = self.day('2026-10-26')[0]
        completed = self.day('2026-11-01')[0]
        self.assertEqual(self.complete(completed)[0].status_code, 200)
        item = route+'/'+str(rule['id'])
        for method in ('put', 'delete'):
            self.assertEqual(getattr(self.c, method)(item, headers=self.agent, json={}).status_code, 400)
        invalid = self.c.put(item, headers=self.agent, json=self.body(expected_version=1, end_minute=720))
        self.assertEqual(invalid.status_code, 400)
        changed = self.c.put(item, headers=self.agent, json=self.body(title='English', weekdays=[1,7],
                            valid_from=rule['valid_from'], valid_until=rule['valid_until'], start_minute=13*60,
                            end_minute=14*60, expected_version=1))
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json['rule']['version'], 2)
        self.assertEqual(changed.json['rule']['valid_until'], '2026-12-31')
        self.assertEqual(self.day()[0], started)
        self.assertEqual(self.day('2026-11-01')[0]['title'], completed['title'])
        self.assertEqual(self.day('2026-10-26')[0]['title'], 'English')
        self.assertEqual(self.day('2026-10-26')[0]['start_ms'], future['start_ms']+3600000)
        for method in ('put', 'delete'):
            stale = getattr(self.c, method)(item, headers=self.agent, json=self.body(expected_version=1))
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json['rule']['version'], 2)
        self.assertEqual(self.c.get(self.base, headers=self.owner).json['rules'], [changed.json['rule']])

    def test_agent_and_management_rule_creates_deduplicate_in_one_store(self):
        route = '/api/android/agent/schedule/rules'
        first = self.c.post(route, headers=self.agent, json=self.body())
        self.assertEqual(first.status_code, 200)
        duplicate = self.create()
        self.assertEqual(duplicate, first.json['rule'])
        self.assertEqual(len(self.day()), 1)
        changed = self.c.put(self.base+'/rules/'+str(duplicate['id']), headers=self.owner,
                             json=self.body(title='changed by owner', expected_version=1))
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.c.get(route, headers=self.agent).json['rules'], [changed.json['rule']])

    def test_validation_dates_times_and_exact_fields(self):
        for changes in ({'title': ''}, {'title': 'x'*81}, {'notes': 'x'*501}, {'weekdays': []}, {'weekdays': [True]}, {'weekdays': [0,8]}, {'weekdays': [7,7]}, {'weekdays': [7,1]}, {'start_minute': True}, {'start_minute': 1439}, {'end_minute': 1440}, {'end_minute': 720}, {'valid_from': '2026-2-1'}, {'valid_from': '2026-02-30'}, {'valid_from': '2026-10-26', 'valid_until': '2026-10-25'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.c.post(self.base+'/rules', headers=self.owner, json=self.body(**changes)).status_code, 400)
        rule = self.create()
        self.assertEqual(set(rule), {'id','title','notes','weekdays','start_minute','end_minute','valid_from','valid_until','version','active'})
        row = self.day()[0]
        self.assertEqual(set(row), {'id','rule_id','local_date','timezone','start_ms','end_ms','title','notes','status','version','completed_ms'})
        self.assertEqual(row['start_ms'], ms('2026-10-25T04:00:00'))
        self.assertEqual(row['end_ms'], ms('2026-10-25T05:00:00'))
        self.assertEqual(rule['valid_from'], '2026-10-25')

    def test_initial_ongoing_but_not_ended_and_identical_create_dedup(self):
        self.create(start_minute=11*60, end_minute=13*60)
        self.create(start_minute=10*60, end_minute=12*60)
        a = self.create(start_minute=12*60+1)
        b = self.create(start_minute=12*60+1)
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(len(self.day()), 2)
        self.assertEqual(len(self.c.get(self.base, headers=self.owner).json['rules']), 3)

    def test_edits_keep_current_completed_and_cancel_removed_future(self):
        rule = self.create(weekdays=[1,7])
        first = self.day()[0]
        future = self.day('2026-10-26')[0]
        later = self.day('2026-11-01')[0]
        self.assertEqual(self.complete(later)[0].status_code, 200)
        updated = self.c.put(self.base+f'/rules/{rule["id"]}', headers=self.owner, json=self.body(title='物理', weekdays=[7], expected_version=1))
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.day()[0], first)
        self.assertEqual(self.day('2026-10-26')[0]['status'], 'cancelled')
        self.assertGreater(self.day('2026-10-26')[0]['version'], future['version'])
        self.assertEqual(self.day('2026-11-01')[0]['title'], later['title'])
        changed = self.day('2026-11-08')[0]
        self.assertEqual(changed['title'], '物理')
        stale = self.c.put(self.base+f'/rules/{rule["id"]}', headers=self.owner, json=self.body(expected_version=1))
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json['rule']['version'], 2)
        self.assertEqual(self.c.delete(self.base+f'/rules/{rule["id"]}', headers=self.owner, json={'expected_version': 2}).status_code, 200)
        self.assertEqual(self.day()[0], first)
        self.assertEqual(self.day('2026-11-01')[0]['status'], 'completed')
        self.assertEqual(self.day('2026-11-08')[0]['status'], 'cancelled')
        self.assertEqual(self.c.get(self.base, headers=self.owner).json['rules'], [])

    def test_no_resurrection_after_removed_weekday_or_explicit_cancel(self):
        rule = self.create(weekdays=[1,7])
        future = self.day('2026-10-26')[0]
        for version, weekdays in [(1,[7]),(2,[1,7])]:
            self.assertEqual(self.c.put(self.base+f'/rules/{rule["id"]}', headers=self.owner, json=self.body(weekdays=weekdays, expected_version=version)).status_code, 200)
        self.assertEqual(self.day('2026-10-26')[0]['id'], future['id'])
        self.assertEqual(self.day('2026-10-26')[0]['status'], 'cancelled')

    def test_daily_materialization_keeps_missed_pending_and_advances_window(self):
        self.create(weekdays=[1,2,3,4,5,6,7])
        before = self.snapshot().json
        with patch('dev_control.agenda.now_ms', return_value=ms('2026-12-24T04:00:00')):
            after = self.snapshot().json
            self.assertEqual(after['through_date'], '2027-02-17')
            self.assertGreater(after['revision'], before['revision'])
            self.assertEqual(after['total_count'], 116)
            self.assertEqual(self.day()[0]['status'], 'pending')

    def test_operations_replay_payload_binding_versions_and_cancel_conflict(self):
        self.create()
        row = self.day()[0]
        response, body = self.complete(row)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['duplicate'])
        canonical = response.json['occurrence']
        self.assertEqual(canonical['status'], 'completed')
        self.assertEqual(canonical['completed_ms'], ms('2026-10-25T04:00:00'))
        self.assertGreater(canonical['version'], row['version'])
        undo, _ = self.complete(canonical, completed=False)
        self.assertEqual(undo.status_code, 200)
        replay = self.c.post('/api/android/agent/schedule/completions', headers=self.agent, json=body)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json['duplicate'])
        self.assertEqual(replay.json['occurrence'], undo.json['occurrence'])
        self.assertEqual(self.c.post('/api/android/agent/schedule/completions', headers=self.agent, json={**body, 'completed': False}).status_code, 409)
        self.assertEqual(self.complete(row)[0].status_code, 409)
        current = undo.json['occurrence']
        cancel = self.c.put(self.base+f'/occurrences/{row["id"]}', headers=self.owner, json={'status': 'cancelled', 'expected_version': current['version']})
        self.assertEqual(cancel.status_code, 200)
        conflict = self.complete(cancel.json['occurrence'])[0]
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json['occurrence']['status'], 'cancelled')
        for changes in ({'operation_id': 'INVALID'}, {'completed': 1}, {'expected_version': True}):
            self.assertEqual(self.complete(row, **changes)[0].status_code, 400)

    def test_ack_stale_missing_and_unchanged_after_materialization(self):
        first = self.snapshot().json
        self.assertFalse(first['unchanged'])
        self.assertEqual(self.c.post('/api/android/agent/schedule/ack', headers=self.agent, json={}).status_code, 400)
        self.assertEqual(self.c.post('/api/android/agent/schedule/ack', headers=self.agent, json={'revision': first['revision']+1}).status_code, 409)
        self.assertEqual(self.c.post('/api/android/agent/schedule/ack', headers=self.agent, json={'revision': first['revision']}).status_code, 200)
        state = self.c.get(self.base, headers=self.owner).json
        self.assertEqual(state['ack_revision'], first['revision'])
        self.assertIsNotNone(state['ack_at'])
        self.assertTrue(self.snapshot(known_revision=first['revision']).json['unchanged'])
        self.create()
        self.assertFalse(self.snapshot(known_revision=first['revision']).json['unchanged'])
        newer = self.snapshot().json['revision']
        self.c.post('/api/android/agent/schedule/ack', headers=self.agent, json={'revision': newer})
        self.c.post('/api/android/agent/schedule/ack', headers=self.agent, json={'revision': first['revision']})
        self.assertEqual(self.c.get(self.base, headers=self.owner).json['ack_revision'], newer)

    def test_snapshot_pages_are_ordered_and_revision_coherent(self):
        for i in range(5):
            self.create(title='Course '+str(i), weekdays=[1,2,3,4,5,6,7])
        first = self.snapshot().json
        self.assertEqual(first['total_count'], 280)
        self.assertEqual(len(first['occurrences']), 200)
        self.assertEqual(first['next_after'], first['occurrences'][-1]['id'])
        second = self.snapshot(after_id=first['next_after'], known_revision=first['revision']).json
        self.assertEqual(len(second['occurrences']), 80)
        self.assertIsNone(second['next_after'])
        self.assertEqual(first['revision'], second['revision'])
        self.assertEqual(sorted({r['id'] for r in first['occurrences']+second['occurrences']}), [r['id'] for r in first['occurrences']+second['occurrences']])
        self.create(title='changed')
        self.assertEqual(self.snapshot(after_id=first['next_after'], known_revision=first['revision']).status_code, 409)
        self.assertEqual(self.snapshot(after_id=1).status_code, 400)

    def test_sync_tasks_coalesce_and_legacy_guard(self):
        self.create(title='A')
        self.create(title='B')
        tasks = self.c.get('/api/agent/poll', headers=self.agent).json['tasks']
        self.assertEqual([x['kind'] for x in tasks], ['android.schedule.sync'])
        self.assertEqual([x['kind'] for x in self.c.get('/api/android/agent/recover-tasks', headers=self.agent).json['tasks']], ['android.schedule.sync'])
        old, agent = self.pair('old', capable=False)
        self.assertEqual(self.c.post(f'/api/android/devices/{old}/schedule/rules', headers=self.owner, json=self.body()).status_code, 200)
        self.assertEqual(self.c.get('/api/agent/poll', headers=agent).json['tasks'], [])

    def test_management_overdue_pagination_total_and_server_corrections(self):
        self.create(weekdays=[1,2,3,4,5,6,7])
        with patch('dev_control.agenda.now_ms', return_value=ms('2027-02-24T04:00:00')):
            first = self.c.get(self.base+'/occurrences?overdue=1&limit=20', headers=self.owner).json
            self.assertEqual(len(first['occurrences']), 20)
            self.assertGreater(first['total_count'], 100)
            self.assertIsNotNone(first['next_after'])
            second = self.c.get(self.base+f'/occurrences?overdue=1&limit=20&after_id={first["next_after"]}', headers=self.owner).json
            self.assertTrue(set(r['id'] for r in first['occurrences']).isdisjoint(r['id'] for r in second['occurrences']))
        row = self.day()[0]
        done = self.c.put(self.base+f'/occurrences/{row["id"]}', headers=self.owner, json={'status':'completed', 'expected_version': row['version']})
        self.assertEqual(done.status_code, 200)
        self.assertEqual(self.c.put(self.base+f'/occurrences/{row["id"]}', headers=self.owner, json={'status':'pending', 'expected_version': row['version']}).status_code, 409)
        self.assertEqual(self.c.get(self.base+'/occurrences?date=2026-2-1', headers=self.owner).status_code, 400)

    def test_dst_gap_skipped_and_fold_earlier(self):
        with self.app.app_context():
            get_db().execute('UPDATE android_devices SET timezone=? WHERE device_id=?', ('America/New_York', self.dev)); get_db().commit()
        with patch('dev_control.agenda.now_ms', return_value=ms('2026-03-07T00:00:00')):
            self.create(start_minute=150, end_minute=210, valid_from='2026-03-08', valid_until='2026-03-08')
            self.assertEqual(self.day('2026-03-08'), [])
        with patch('dev_control.agenda.now_ms', return_value=ms('2026-10-30T00:00:00')):
            self.create(start_minute=90, end_minute=150, valid_from='2026-11-01', valid_until='2026-11-01')
            row = self.day('2026-11-01')[0]
            self.assertEqual(row['start_ms'], ms('2026-11-01T05:30:00'))
            self.assertEqual(row['end_ms'], ms('2026-11-01T07:30:00'))

    def test_timezone_change_fills_previously_nonexistent_future_wall_time(self):
        with self.app.app_context():
            get_db().execute('UPDATE android_devices SET timezone=? WHERE device_id=?', ('America/New_York', self.dev)); get_db().commit()
        with patch('dev_control.agenda.now_ms', return_value=ms('2026-03-07T00:00:00')):
            self.create(start_minute=150, end_minute=210, valid_from='2026-03-08', valid_until='2026-03-08')
            self.assertEqual(self.day('2026-03-08'), [])
            with self.app.app_context():
                get_db().execute('UPDATE android_devices SET timezone=? WHERE device_id=?', ('UTC', self.dev)); get_db().commit()
            self.assertEqual(len(self.day('2026-03-08')), 1)
            self.assertEqual(self.day('2026-03-08')[0]['start_ms'], ms('2026-03-08T02:30:00'))

    def test_failed_completion_uuid_remains_bound_to_original_payload(self):
        self.create()
        row = self.day()[0]
        stale, body = self.complete(row, expected_version=row['version']+1)
        self.assertEqual(stale.status_code, 409)
        # Correcting an expected version requires a new operation UUID.
        reuse = self.c.post('/api/android/agent/schedule/completions', headers=self.agent, json={**body, 'expected_version': row['version']})
        self.assertEqual(reuse.status_code, 409)
        replay = self.c.post('/api/android/agent/schedule/completions', headers=self.agent, json=body)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(self.day()[0]['status'], 'pending')

    def test_concurrent_identical_create_has_one_rule_and_one_occurrence_per_date(self):
        barrier = Barrier(3)
        def create():
            with self.app.test_client() as client:
                barrier.wait(timeout=5)
                response = client.post(self.base+'/rules', headers=self.owner, json=self.body())
                return response.status_code, response.json
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _:create(), range(3)))
        self.assertEqual([r[0] for r in results], [200,200,200])
        self.assertEqual(len({r[1]['rule']['id'] for r in results}), 1)
        self.assertEqual(len(self.day()), 1)
        self.assertEqual(len(self.c.get(self.base, headers=self.owner).json['rules']), 1)

    def seed_history(self, rule_id, count, title='old', notes=''):
        with self.app.app_context():
            db = get_db()
            db.executemany('INSERT INTO android_schedule_occurrences(device_id,rule_id,local_date,start_ms,end_ms,title,notes) VALUES(?,?,?,?,?,?,?)',
                           [(self.dev,rule_id,(date(1990,1,1)+timedelta(days=i)).isoformat(),i*86400000,i*86400000+3600000,title,notes) for i in range(count)])
            db.execute('UPDATE android_schedule_state SET revision=revision+1 WHERE device_id=?', (self.dev,))
            db.commit()

    def test_explicit_snapshot_count_and_byte_limits(self):
        rule = self.create()
        self.seed_history(rule['id'], 10001)
        limit = self.snapshot()
        self.assertEqual(limit.status_code, 413)
        self.assertEqual(limit.json['limit'], 10000)
        with self.app.app_context():
            get_db().execute("DELETE FROM android_schedule_occurrences WHERE local_date<'2026-01-01'");get_db().commit()
        self.seed_history(rule['id'], 8000, title='🟢'*80, notes='🟢'*500)
        limit = self.snapshot()
        self.assertEqual(limit.status_code, 413)
        self.assertEqual(limit.json['limit'], 16*1024*1024)

    def test_rule_limit_preserves_identical_create_dedup(self):
        rule = self.create()
        with self.app.app_context():
            db = get_db()
            for i in range(127):
                db.execute('INSERT INTO android_schedule_rules(device_id,title,notes,weekdays_json,start_minute,end_minute,valid_from,created_ms,changed_ms) VALUES(?,?,?,?,?,?,?,?,?)',
                           (self.dev,'seed'+str(i),'','[1]',540,600,'2026-10-25',ms('2026-10-25T04:00:00'),ms('2026-10-25T04:00:00')))
            db.commit()
        self.assertEqual(self.c.post(self.base+'/rules', headers=self.owner, json=self.body(title='beyond limit')).status_code, 413)
        self.assertEqual(self.create()['id'], rule['id'])

    def test_snapshot_keeps_all_old_pending_but_only_current_window_done_history(self):
        self.create(weekdays=[1,2,3,4,5,6,7])
        first = self.day()[0]
        self.complete(first)
        with patch('dev_control.agenda.now_ms', return_value=ms('2026-12-24T04:00:00')):
            snapshot = self.snapshot().json
            self.assertNotIn(first['id'], [r['id'] for r in snapshot['occurrences']])
            self.assertIn('2026-10-26', [r['local_date'] for r in snapshot['occurrences']])
            self.assertEqual(snapshot['week_start'], '2026-12-21')
            self.assertEqual(snapshot['total_count'], 115)

    def test_occurrence_timezone_preserves_started_completed_and_cancelled_history(self):
        self.create(weekdays=[1,2,3,4,5,6,7])
        started = self.day()[0]
        completed = self.complete(self.day('2026-10-26')[0])[0].json['occurrence']
        cancelled_row = self.day('2026-10-27')[0]
        cancelled = self.c.put(self.base+f'/occurrences/{cancelled_row["id"]}', headers=self.owner,
                               json={'status':'cancelled','expected_version':cancelled_row['version']}).json['occurrence']
        future = self.day('2026-10-28')[0]
        self.assertEqual(started.get('timezone'), 'Asia/Shanghai')
        with self.app.app_context():
            get_db().execute('UPDATE android_devices SET timezone=? WHERE device_id=?', ('America/Los_Angeles',self.dev));get_db().commit()
        snapshot = self.snapshot().json
        rows = {r['id']:r for r in snapshot['occurrences']}
        self.assertEqual(snapshot['timezone'], 'America/Los_Angeles')
        for original in (started,completed,cancelled):
            self.assertEqual(rows[original['id']], original)
            self.assertEqual(rows[original['id']]['timezone'], 'Asia/Shanghai')
        updated = rows[future['id']]
        self.assertEqual(updated['timezone'], 'America/Los_Angeles')
        self.assertGreater(updated['version'], future['version'])
        self.assertEqual(updated['local_date'], future['local_date'])
        self.assertNotEqual(updated['start_ms'], future['start_ms'])

    def test_explicit_reopen_survives_deleted_rule_repeated_reads_and_agent_sync(self):
        rule = self.create(weekdays=[1,7])
        future = self.day('2026-10-26')[0]
        self.assertEqual(self.c.delete(self.base+f'/rules/{rule["id"]}', headers=self.owner,
                                      json={'expected_version':rule['version']}).status_code, 200)
        cancelled = self.day('2026-10-26')[0]
        self.assertEqual(cancelled['status'], 'cancelled')
        response = self.c.put(self.base+f'/occurrences/{future["id"]}', headers=self.owner,
                              json={'status':'pending','expected_version':cancelled['version']})
        self.assertEqual(response.status_code, 200)
        reopened = response.json['occurrence']
        self.assertEqual(reopened['status'], 'pending')
        for _ in range(2):
            self.assertEqual(self.day('2026-10-26')[0], reopened)
        self.assertEqual(next(r for r in self.snapshot().json['occurrences'] if r['id']==future['id']), reopened)
        done = self.complete(reopened)[0]
        self.assertEqual(done.status_code, 200)
        undo = self.complete(done.json['occurrence'], completed=False)[0]
        self.assertEqual(undo.status_code, 200)
        self.assertEqual(self.day('2026-10-26')[0]['status'], 'pending')

    def test_new_explicit_weekly_edit_supersedes_only_future_pending_reopen_overrides(self):
        rule = self.create(weekdays=[1,7])
        self.assertEqual(self.c.put(self.base+f'/rules/{rule["id"]}', headers=self.owner,
                                   json=self.body(weekdays=[7],expected_version=1)).status_code, 200)
        cancelled = self.day('2026-10-26')[0]
        reopen = self.c.put(self.base+f'/occurrences/{cancelled["id"]}', headers=self.owner,
                            json={'status':'pending','expected_version':cancelled['version']})
        self.assertEqual(reopen.status_code, 200)
        self.assertEqual(self.day('2026-10-26')[0]['status'], 'pending')
        self.assertEqual(self.c.put(self.base+f'/rules/{rule["id"]}', headers=self.owner,
                                   json=self.body(title='new plan',weekdays=[7],expected_version=2)).status_code, 200)
        self.assertEqual(self.day('2026-10-26')[0]['status'], 'cancelled')
        cancelled = self.day('2026-10-26')[0]
        self.c.put(self.base+f'/occurrences/{cancelled["id"]}', headers=self.owner,
                   json={'status':'pending','expected_version':cancelled['version']})
        with patch('dev_control.agenda.now_ms', return_value=ms('2026-10-26T04:00:00')):
            self.assertEqual(self.c.delete(self.base+f'/rules/{rule["id"]}', headers=self.owner,
                                          json={'expected_version':3}).status_code, 200)
            self.assertEqual(self.day('2026-10-26')[0]['status'], 'pending')

    def test_device_undo_for_completed_future_removed_rule_returns_durable_conflict(self):
        for index, delete in enumerate((False,True)):
            rule = self.create(title='case'+str(index),weekdays=[1,7])
            future = next(r for r in self.day('2026-10-26') if r['rule_id']==rule['id'])
            completed = self.complete(future)[0].json['occurrence']
            path = self.base+f'/rules/{rule["id"]}'
            response = self.c.delete(path,headers=self.owner,json={'expected_version':1}) if delete else self.c.put(path,headers=self.owner,json=self.body(title='case'+str(index),weekdays=[7],expected_version=1))
            self.assertEqual(response.status_code,200)
            conflict,body = self.complete(completed,completed=False)
            self.assertEqual(conflict.status_code,409)
            self.assertEqual(conflict.json['occurrence'],completed)
            self.assertEqual(self.c.post('/api/android/agent/schedule/completions',headers=self.agent,json=body).status_code,409)
            self.assertEqual(next(r for r in self.day('2026-10-26') if r['id']==future['id']),completed)

    def test_additive_agenda_migration_backfills_timezone_without_changing_auth(self):
        self.create()
        row = self.day()[0]
        revision = self.snapshot().json['revision']
        with self.app.app_context():
            db=get_db()
            users=[tuple(r) for r in db.execute('SELECT * FROM users ORDER BY id')]
            devices=[tuple(r) for r in db.execute('SELECT * FROM devices ORDER BY id')]
            columns={r[1] for r in db.execute('PRAGMA table_info(android_schedule_occurrences)')}
            for name in ('timezone','manual_override'):
                if name in columns:
                    db.execute('ALTER TABLE android_schedule_occurrences DROP COLUMN '+name)
            db.commit()
        migrated=create_app(dict(self.app.config))
        with migrated.app_context():
            db=get_db()
            columns={r[1] for r in db.execute('PRAGMA table_info(android_schedule_occurrences)')}
            self.assertTrue({'timezone','manual_override'} <= columns)
            updated=db.execute('SELECT * FROM android_schedule_occurrences WHERE id=?',(row['id'],)).fetchone()
            self.assertEqual(updated['timezone'],'Asia/Shanghai')
            self.assertEqual(updated['version'],row['version']+1)
            self.assertEqual(updated['manual_override'],0)
            self.assertGreater(db.execute('SELECT revision FROM android_schedule_state WHERE device_id=?',(self.dev,)).fetchone()[0],revision)
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM users ORDER BY id')],users)
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM devices ORDER BY id')],devices)

    def test_openapi_documents_agenda_routes_and_device_auth(self):
        spec = self.c.get('/api/openapi.json', headers=self.owner).json
        paths = spec['paths']
        base = '/api/android/devices/{device_id}/schedule'
        for suffix in ('', '/rules', '/rules/{rule_id}', '/occurrences', '/occurrences/{occurrence_id}'):
            self.assertIn(base+suffix, paths)
        for suffix, method in (('', 'get'), ('/ack', 'post'), ('/completions', 'post')):
            self.assertEqual(paths['/api/android/agent/schedule'+suffix][method]['security'], [{'deviceToken': []}])

    def test_navigation_has_sidebar_agenda_without_top_tabs(self):
        html = self.c.get('/').get_data(as_text=True)
        self.assertNotIn('tabbar', html)
        self.assertNotIn('tabButtons', html)
        self.assertIn('课程日程', html)
        self.assertIn('/android/schedule/', html)
