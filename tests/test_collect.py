"""核对实例采集：重复规则展开、例外与调期、时区解释、版本比对与缓存。"""
import base64
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import service
from app.collect import StateStore, collect_response
from app.lab import SourceServer
from app.source import from_packet

W0 = '2026-03-01T00:00:00Z'
W1 = '2026-04-01T00:00:00Z'


def ev(*lines):
    return 'BEGIN:VEVENT\r\n' + '\r\n'.join(lines) + '\r\nEND:VEVENT\r\n'


def ics(*parts):
    return 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\n' + ''.join(parts) + 'END:VCALENDAR\r\n'


def packet(body, source_id='src', etag=None):
    headers = [['Content-Type', 'text/calendar']]
    if etag:
        headers.append(['ETag', etag])
    return {'source_id': source_id, 'url': 'https://example.test/ics', 'status': 200,
            'headers': headers, 'body_base64': base64.b64encode(body.encode('utf-8')).decode()}


def collect(body, w0=W0, w1=W1, state=None, source_id='src', floating_tz=None, tz_map=None):
    return service.collect_packet(packet(body, source_id), window_start=w0, window_end=w1,
                                  floating_tz=floating_tz, tz_map=tz_map,
                                  state=state if state is not None else StateStore())


def starts(result):
    return [i['start_utc'] for i in result['instances']]


class ExpansionTests(unittest.TestCase):
    def test_weekly_count_basic(self):
        body = ics(ev('UID:talk-a', 'DTSTART:20260302T090000Z', 'SUMMARY:Weekly sync',
                      'RRULE:FREQ=WEEKLY;COUNT=3'))
        result = collect(body)
        self.assertTrue(result['complete'])
        self.assertEqual(starts(result), ['2026-03-02T09:00:00Z', '2026-03-09T09:00:00Z',
                                          '2026-03-16T09:00:00Z'])
        first = result['instances'][0]
        self.assertEqual(first['key'], {'source_id': 'src', 'uid': 'talk-a',
                                        'recurrence_id': 'Z:2026-03-02T09:00:00Z'})
        self.assertEqual(first['provenance']['role'], 'rrule')
        self.assertEqual(first['provenance']['response_sha256'], result['version']['response_sha256'])
        self.assertTrue(first['in_window'])

    def test_count_consumes_occurrences_before_window(self):
        body = ics(ev('UID:e1', 'DTSTART:20260225T090000Z', 'RRULE:FREQ=WEEKLY;COUNT=2'))
        result = collect(body)
        self.assertEqual(starts(result), ['2026-03-04T09:00:00Z'])

    def test_until_inclusive_utc_form(self):
        body = ics(ev('UID:e1', 'DTSTART:20260301T090000Z', 'RRULE:FREQ=DAILY;UNTIL=20260303T090000Z'))
        self.assertEqual(starts(collect(body)),
                         ['2026-03-01T09:00:00Z', '2026-03-02T09:00:00Z', '2026-03-03T09:00:00Z'])

    def test_infinite_rule_bounded_by_window(self):
        body = ics(ev('UID:e1', 'DTSTART:20200101T090000Z', 'RRULE:FREQ=DAILY'))
        result = collect(body, w1='2026-03-04T00:00:00Z')
        self.assertEqual(starts(result), ['2026-03-01T09:00:00Z', '2026-03-02T09:00:00Z',
                                          '2026-03-03T09:00:00Z'])

    def test_monthly_bysetpos_last_weekday(self):
        body = ics(ev('UID:e1', 'DTSTART:20260301T090000Z',
                      'RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;COUNT=3'))
        self.assertEqual(starts(collect(body, w1='2026-06-01T00:00:00Z')),
                         ['2026-03-31T09:00:00Z', '2026-04-30T09:00:00Z', '2026-05-29T09:00:00Z'])

    def test_monthly_ordinal_byday_and_interval(self):
        body = ics(ev('UID:e1', 'DTSTART:20260301T090000Z', 'RRULE:FREQ=MONTHLY;BYDAY=2TU;COUNT=2'))
        self.assertEqual(starts(collect(body, w1='2026-05-01T00:00:00Z')),
                         ['2026-03-10T09:00:00Z', '2026-04-14T09:00:00Z'])

    def test_wkst_shifts_interval_weeks(self):
        template = 'RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=SU;WKST={}'
        monday = collect(ics(ev('UID:e1', 'DTSTART:20260302T090000Z', template.format('MO'))),
                         w1='2026-04-15T00:00:00Z')
        sunday = collect(ics(ev('UID:e1', 'DTSTART:20260302T090000Z', template.format('SU'))),
                         w1='2026-04-15T00:00:00Z')
        self.assertEqual(starts(monday), ['2026-03-08T09:00:00Z', '2026-03-22T09:00:00Z',
                                          '2026-04-05T09:00:00Z'])
        self.assertEqual(starts(sunday), ['2026-03-15T09:00:00Z', '2026-03-29T09:00:00Z',
                                          '2026-04-12T09:00:00Z'])

    def test_rdate_adds_and_exdate_removes(self):
        body = ics(ev('UID:e1', 'DTSTART:20260302T090000Z', 'RRULE:FREQ=WEEKLY;COUNT=3',
                      'EXDATE:20260309T090000Z', 'RDATE:20260320T090000Z'))
        result = collect(body)
        self.assertEqual(starts(result), ['2026-03-02T09:00:00Z', '2026-03-16T09:00:00Z',
                                          '2026-03-20T09:00:00Z'])
        roles = {i['start_utc']: i['provenance']['role'] for i in result['instances']}
        self.assertEqual(roles['2026-03-20T09:00:00Z'], 'rdate')

    def test_rdate_with_own_tzid(self):
        body = ics(ev('UID:e1', 'DTSTART:20260302T090000Z', 'RRULE:FREQ=WEEKLY;COUNT=1',
                      'RDATE;TZID=Asia/Shanghai:20260310T090000'))
        result = collect(body)
        self.assertEqual(starts(result), ['2026-03-02T09:00:00Z', '2026-03-10T01:00:00Z'])

    def test_window_is_half_open(self):
        body = ics(ev('UID:e1', 'DTSTART:20260310T000000Z'))
        self.assertEqual(starts(collect(body, w1='2026-03-10T00:00:00Z')), [])
        self.assertEqual(starts(collect(body, w0='2026-03-10T00:00:00Z')), ['2026-03-10T00:00:00Z'])


class TimezoneTests(unittest.TestCase):
    def test_weekly_rule_keeps_wall_clock_across_dst(self):
        body = ics(ev('UID:e1', 'DTSTART;TZID=America/New_York:20260302T090000',
                      'RRULE:FREQ=WEEKLY;COUNT=3'))
        result = collect(body)
        self.assertEqual(starts(result), ['2026-03-02T14:00:00Z', '2026-03-09T13:00:00Z',
                                          '2026-03-16T13:00:00Z'])
        self.assertEqual(result['instances'][0]['tzid'], 'America/New_York')

    def test_all_day_date_uses_registered_local_zone(self):
        body = ics(ev('UID:e1', 'DTSTART;VALUE=DATE:20260310', 'SUMMARY:Fair'))
        result = collect(body, floating_tz='Asia/Shanghai')
        only = result['instances'][0]
        self.assertTrue(only['all_day'])
        self.assertEqual(only['start_utc'], '2026-03-09T16:00:00Z')
        self.assertEqual(only['end_utc'], '2026-03-10T16:00:00Z')
        self.assertEqual(only['key']['recurrence_id'], 'D:2026-03-10')

    def test_floating_datetime_uses_registered_zone(self):
        body = ics(ev('UID:e1', 'DTSTART:20260310T090000'))
        result = collect(body, floating_tz='Asia/Shanghai')
        self.assertEqual(starts(result), ['2026-03-10T01:00:00Z'])
        self.assertEqual(result['instances'][0]['tzid'], 'floating:Asia/Shanghai')

    def test_floating_without_registered_zone_flagged(self):
        body = ics(ev('UID:e1', 'DTSTART:20260310T090000'))
        result = collect(body)
        self.assertFalse(result['complete'])
        self.assertEqual([u['reason'] for u in result['unexpanded']], ['no-floating-tz'])

    def test_unknown_tzid_flagged_not_dropped_silently(self):
        body = ics(ev('UID:e1', 'DTSTART;TZID=Nowhere/Zone:20260310T090000'),
                   ev('UID:e2', 'DTSTART:20260310T090000Z'))
        result = collect(body)
        self.assertFalse(result['complete'])
        self.assertEqual([u['reason'] for u in result['unexpanded']], ['unknown-tz'])
        self.assertEqual([i['key']['uid'] for i in result['instances']], ['e2'])

    def test_source_vtimezone_used_when_not_in_map(self):
        vtz = ('BEGIN:VTIMEZONE\r\nTZID:Custom/Zone\r\nBEGIN:STANDARD\r\n'
               'DTSTART:19670101T000000\r\nTZOFFSETFROM:+0230\r\nTZOFFSETTO:+0230\r\n'
               'END:STANDARD\r\nEND:VTIMEZONE\r\n')
        body = ics(vtz, ev('UID:e1', 'DTSTART;TZID=Custom/Zone:20260310T090000'))
        self.assertEqual(starts(collect(body)), ['2026-03-10T06:30:00Z'])

    def test_source_vtimezone_matches_iana_dst(self):
        vtz = ('BEGIN:VTIMEZONE\r\nTZID:Custom/York\r\n'
               'BEGIN:DAYLIGHT\r\nDTSTART:20070311T020000\r\n'
               'RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\n'
               'TZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\nEND:DAYLIGHT\r\n'
               'BEGIN:STANDARD\r\nDTSTART:20071104T020000\r\n'
               'RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n'
               'TZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n')
        body = ics(vtz, ev('UID:e1', 'DTSTART;TZID=Custom/York:20260302T093000',
                           'RRULE:FREQ=WEEKLY;COUNT=3'))
        result = collect(body)
        self.assertEqual(starts(result), ['2026-03-02T14:30:00Z', '2026-03-09T13:30:00Z',
                                          '2026-03-16T13:30:00Z'])
        reference = ZoneInfo('America/New_York')
        for i in result['instances']:
            wall_text = i['key']['recurrence_id'].removeprefix('T:Custom/York:')
            wall = datetime.strptime(wall_text, '%Y-%m-%dT%H:%M:%S')
            expect = wall.replace(tzinfo=reference).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            self.assertEqual(i['start_utc'], expect)

    def test_conflicting_vtimezone_and_frozen_map_reported(self):
        vtz = ('BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\nBEGIN:STANDARD\r\n'
               'DTSTART:19670101T000000\r\nTZOFFSETFROM:+0500\r\nTZOFFSETTO:+0500\r\n'
               'END:STANDARD\r\nEND:VTIMEZONE\r\n')
        body = ics(vtz, ev('UID:e1', 'DTSTART;TZID=Europe/Berlin:20260310T090000'))
        result = collect(body, tz_map={'Europe/Berlin': ZoneInfo('Europe/Berlin')})
        self.assertFalse(result['complete'])
        self.assertEqual([u['reason'] for u in result['unexpanded']], ['tz-conflict'])


class ExceptionTests(unittest.TestCase):
    def test_exception_moves_instance_within_window(self):
        body = ics(ev('UID:e1', 'DTSTART:20260302T090000Z', 'SUMMARY:Daily',
                      'RRULE:FREQ=DAILY;COUNT=5'),
                   ev('UID:e1', 'RECURRENCE-ID:20260304T090000Z', 'DTSTART:20260304T163000Z',
                      'SUMMARY:Daily moved'))
        result = collect(body)
        moved = [i for i in result['instances'] if i['provenance']['role'] == 'exception']
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]['key']['recurrence_id'], 'Z:2026-03-04T09:00:00Z')
        self.assertEqual(moved[0]['start_utc'], '2026-03-04T16:30:00Z')
        self.assertEqual(moved[0]['original_start_utc'], '2026-03-04T09:00:00Z')
        self.assertEqual(moved[0]['summary'], 'Daily moved')
        self.assertEqual(len(result['instances']), 5)

    def test_exception_moved_out_of_window_is_kept_as_shadow(self):
        body = ics(ev('UID:e1', 'DTSTART:20260302T090000Z', 'RRULE:FREQ=DAILY;COUNT=7'),
                   ev('UID:e1', 'RECURRENCE-ID:20260305T090000Z', 'DTSTART:20260401T090000Z'))
        result = collect(body)
        shadows = [i for i in result['instances'] if i['window_role'] == 'shadow']
        self.assertEqual(len(shadows), 1)
        shadow = shadows[0]
        self.assertEqual(shadow['key']['recurrence_id'], 'Z:2026-03-05T09:00:00Z')
        self.assertEqual(shadow['start_utc'], '2026-04-01T09:00:00Z')
        self.assertEqual(shadow['original_start_utc'], '2026-03-05T09:00:00Z')
        self.assertFalse(shadow['in_window'])
        effective = [i['start_utc'] for i in result['instances'] if i['in_window']]
        self.assertNotIn('2026-03-05T09:00:00Z', effective)
        self.assertEqual(len(effective), 6)

    def test_cancelled_exception_stays_visible(self):
        body = ics(ev('UID:e1', 'DTSTART:20260302T090000Z', 'RRULE:FREQ=DAILY;COUNT=5'),
                   ev('UID:e1', 'RECURRENCE-ID:20260304T090000Z', 'DTSTART:20260304T090000Z',
                      'STATUS:CANCELLED'))
        result = collect(body)
        cancelled = [i for i in result['instances'] if i['status'] == 'cancelled']
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]['key']['recurrence_id'], 'Z:2026-03-04T09:00:00Z')
        self.assertEqual(cancelled[0]['start_utc'], '2026-03-04T09:00:00Z')
        self.assertFalse(cancelled[0]['in_window'])
        self.assertEqual(len([i for i in result['instances'] if i['in_window']]), 4)

    def test_thisandfuture_reschedule_splits_series(self):
        body = ics(ev('UID:e1', 'DTSTART:20260304T090000Z', 'SUMMARY:Wed sync',
                      'RRULE:FREQ=WEEKLY'),
                   ev('UID:e1', 'RECURRENCE-ID;RANGE=THISANDFUTURE:20260311T090000Z',
                      'DTSTART:20260312T100000Z', 'SUMMARY:Thu sync', 'RRULE:FREQ=WEEKLY'))
        result = collect(body, w1='2026-03-31T00:00:00Z')
        effective = [(i['start_utc'], i['provenance']['role']) for i in result['instances']
                     if i['in_window']]
        self.assertEqual(effective, [('2026-03-04T09:00:00Z', 'rrule'),
                                     ('2026-03-12T10:00:00Z', 'thisandfuture'),
                                     ('2026-03-19T10:00:00Z', 'thisandfuture'),
                                     ('2026-03-26T10:00:00Z', 'thisandfuture')])
        self.assertEqual([i['summary'] for i in result['instances'] if i['in_window']][1],
                         'Thu sync')

    def test_thisandfuture_moved_out_of_window_keeps_change(self):
        body = ics(ev('UID:e1', 'DTSTART:20260304T090000Z', 'RRULE:FREQ=WEEKLY'),
                   ev('UID:e1', 'RECURRENCE-ID;RANGE=THISANDFUTURE:20260311T090000Z',
                      'DTSTART:20270401T100000Z', 'RRULE:FREQ=WEEKLY'))
        result = collect(body, w1='2026-03-31T00:00:00Z')
        shadows = [i for i in result['instances'] if i['window_role'] == 'shadow']
        self.assertEqual(len(shadows), 1)
        self.assertEqual(shadows[0]['key']['recurrence_id'], 'Z:2026-03-11T09:00:00Z')
        self.assertEqual(shadows[0]['start_utc'], '2027-04-01T10:00:00Z')
        self.assertEqual(shadows[0]['original_start_utc'], '2026-03-11T09:00:00Z')
        self.assertEqual([i['start_utc'] for i in result['instances'] if i['in_window']],
                         ['2026-03-04T09:00:00Z'])

    def test_thisandfuture_cancel_truncates_and_reports(self):
        body = ics(ev('UID:e1', 'DTSTART:20260304T090000Z', 'RRULE:FREQ=WEEKLY'),
                   ev('UID:e1', 'RECURRENCE-ID;RANGE=THISANDFUTURE:20260311T090000Z',
                      'DTSTART:20260311T090000Z', 'STATUS:CANCELLED'))
        result = collect(body, w1='2026-03-31T00:00:00Z')
        effective = [i for i in result['instances'] if i['in_window']]
        self.assertEqual([i['start_utc'] for i in effective], ['2026-03-04T09:00:00Z'])
        cancelled = [i for i in result['instances'] if i['status'] == 'cancelled']
        self.assertEqual([i['start_utc'] for i in cancelled],
                         ['2026-03-11T09:00:00Z', '2026-03-18T09:00:00Z', '2026-03-25T09:00:00Z'])


    def test_long_event_starting_before_window_overlaps(self):
        body = ics(ev('UID:e1', 'DTSTART:20260225T090000Z', 'DURATION:P10D'))
        result = collect(body)
        self.assertEqual(starts(result), ['2026-02-25T09:00:00Z'])
        self.assertEqual(result['instances'][0]['end_utc'], '2026-03-07T09:00:00Z')

    def test_exception_moved_into_window_is_effective(self):
        body = ics(ev('UID:e1', 'DTSTART:20260410T090000Z', 'RRULE:FREQ=DAILY;COUNT=3'),
                   ev('UID:e1', 'RECURRENCE-ID:20260410T090000Z', 'DTSTART:20260315T090000Z'))
        result = collect(body)
        self.assertEqual([(i['start_utc'], i['window_role']) for i in result['instances']],
                         [('2026-03-15T09:00:00Z', 'effective')])

    def test_cancelled_master_marks_window_occurrences(self):
        body = ics(ev('UID:e1', 'DTSTART:20260302T090000Z', 'RRULE:FREQ=DAILY;COUNT=2',
                      'STATUS:CANCELLED'))
        result = collect(body)
        self.assertEqual([(i['start_utc'], i['status'], i['in_window']) for i in result['instances']],
                         [('2026-03-02T09:00:00Z', 'cancelled', False),
                          ('2026-03-03T09:00:00Z', 'cancelled', False)])


class GuardTests(unittest.TestCase):
    def test_unsupported_rule_flagged(self):
        for rule in ('RRULE:FREQ=YEARLY', 'RRULE:FREQ=DAILY;BYHOUR=9'):
            with self.subTest(rule=rule):
                body = ics(ev('UID:e1', 'DTSTART:20260310T090000Z', rule))
                result = collect(body)
                self.assertFalse(result['complete'])
                self.assertEqual([u['reason'] for u in result['unexpanded']], ['unsupported-rule'])
                self.assertEqual(result['instances'], [])

    def test_duplicate_master_rejected_regardless_of_file_order(self):
        first = ev('UID:e1', 'DTSTART:20260310T090000Z')
        second = ev('UID:e1', 'DTSTART:20260311T090000Z')
        for body in (ics(first, second), ics(second, first)):
            with self.subTest(body=body):
                result = collect(body)
                self.assertEqual([u['reason'] for u in result['unexpanded']], ['duplicate-master'])

    def test_missing_uid_and_missing_dtstart_flagged(self):
        body = ics(ev('DTSTART:20260310T090000Z'), ev('UID:e2', 'SUMMARY:No start'))
        result = collect(body)
        reasons = sorted(u['reason'] for u in result['unexpanded'])
        self.assertEqual(reasons, ['missing-dtstart', 'missing-uid'])

    def test_parse_error_is_not_treated_as_complete(self):
        result = collect('this is not a calendar')
        self.assertFalse(result['complete'])
        self.assertTrue(result['errors'][0].startswith('parse-error'))
        self.assertEqual(result['instances'], [])


class VersionAndStateTests(unittest.TestCase):
    def test_same_version_not_delivered_twice(self):
        state = StateStore()
        body = ics(ev('UID:e1', 'DTSTART:20260310T090000Z'))
        first = collect(body, state=state)
        self.assertFalse(first['unchanged'])
        second = collect(body, state=state)
        self.assertTrue(second['unchanged'])
        self.assertNotIn('instances', second)

    def test_version_change_distinguishes_add_revoke_modify(self):
        state = StateStore()
        v1 = ics(ev('UID:e1', 'DTSTART:20260305T090000Z', 'SUMMARY:Alpha'),
                 ev('UID:e2', 'DTSTART:20260306T090000Z', 'SUMMARY:Beta'))
        v2 = ics(ev('UID:e1', 'DTSTART:20260305T090000Z', 'SUMMARY:Alpha renamed'),
                 ev('UID:e3', 'DTSTART:20260307T090000Z', 'SUMMARY:Gamma'))
        r1 = collect(v1, state=state)
        self.assertTrue(r1['initial'])
        self.assertIsNone(r1['changes'])
        r2 = collect(v2, state=state)
        self.assertFalse(r2['initial'])
        changes = r2['changes']
        self.assertEqual([i['key']['uid'] for i in changes['added']], ['e3'])
        self.assertEqual([i['key']['uid'] for i in changes['revoked']], ['e2'])
        self.assertEqual([m['key']['uid'] for m in changes['modified']], ['e1'])
        self.assertEqual(changes['modified'][0]['previous']['summary'], 'Alpha')
        self.assertEqual(changes['modified'][0]['current']['summary'], 'Alpha renamed')
        revoked = changes['revoked'][0]
        self.assertEqual(revoked['provenance']['response_sha256'],
                         r1['version']['response_sha256'])
        self.assertEqual(revoked['key']['source_id'], 'src')

    def test_incomplete_collection_not_committed(self):
        state = StateStore()
        body = ics(ev('UID:e1', 'DTSTART;TZID=Nowhere/Zone:20260310T090000'))
        first = collect(body, state=state)
        self.assertFalse(first['complete'])
        retry = collect(body, state=state)
        self.assertFalse(retry['unchanged'])
        fixed = collect(body, state=state, tz_map={'Nowhere/Zone': ZoneInfo('UTC')})
        self.assertTrue(fixed['complete'])
        self.assertEqual(len(fixed['instances']), 1)
        again = collect(body, state=state, tz_map={'Nowhere/Zone': ZoneInfo('UTC')})
        self.assertTrue(again['unchanged'])

    def test_uid_not_unique_across_sources(self):
        state = StateStore()
        body = ics(ev('UID:shared', 'DTSTART:20260310T090000Z'))
        ra = collect(body, state=state, source_id='src-a')
        rb = collect(body, state=state, source_id='src-b')
        self.assertFalse(rb['unchanged'])
        self.assertEqual(ra['instances'][0]['key']['source_id'], 'src-a')
        self.assertEqual(rb['instances'][0]['key']['source_id'], 'src-b')
        changed = ics(ev('UID:shared', 'DTSTART:20260310T090000Z', 'SUMMARY:Renamed'))
        rc = collect(changed, state=state, source_id='src-a')
        self.assertEqual([m['key']['uid'] for m in rc['changes']['modified']], ['shared'])
        rd = collect(body, state=state, source_id='src-b')
        self.assertTrue(rd['unchanged'])

    def test_state_persists_across_store_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'state.json')
            body = ics(ev('UID:e1', 'DTSTART:20260310T090000Z'))
            first = collect(body, state=StateStore(path))
            self.assertFalse(first['unchanged'])
            second = collect(body, state=StateStore(path))
            self.assertTrue(second['unchanged'])

    def test_http_fetch_then_collect_flow(self):
        v1 = ics(ev('UID:e1', 'DTSTART:20260310T090000Z', 'SUMMARY:One'))
        v2 = ics(ev('UID:e1', 'DTSTART:20260310T090000Z', 'SUMMARY:One'),
                 ev('UID:e2', 'DTSTART:20260311T090000Z', 'SUMMARY:Two'))
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / 'state.json')
            with SourceServer(packet(v1)) as server:
                r1 = service.collect_window(server.url, 'src', window_start=W0, window_end=W1,
                                            state_path=state_path)
                self.assertTrue(r1['complete'])
                self.assertEqual(len(r1['instances']), 1)
                server.packet = packet(v2)
                r2 = service.collect_window(server.url, 'src', window_start=W0, window_end=W1,
                                            state_path=state_path)
                self.assertEqual([i['key']['uid'] for i in r2['changes']['added']], ['e2'])
                r3 = service.collect_window(server.url, 'src', window_start=W0, window_end=W1,
                                            state_path=state_path)
                self.assertTrue(r3['unchanged'])
                self.assertEqual(len(server.requests), 3)


if __name__ == '__main__':
    unittest.main()
