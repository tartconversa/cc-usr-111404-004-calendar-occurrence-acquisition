"""按 UID 归组 VEVENT，应用 RRULE/RDATE/EXDATE 与 RECURRENCE-ID 例外，产出带原始键的实例。"""
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone

from . import rrule as rrule_mod
from .icalparse import (CalTime, ParseError, from_utc, iso_z, parse_caltime,
                        parse_duration, to_utc, unescape_text)


class SeriesProblem(Exception):
    """系列无法展开；reason 供归类上报，绝不静默少采。"""

    def __init__(self, reason, detail=''):
        super().__init__(detail or reason)
        self.reason = reason


@dataclass
class EventComponent:
    index: int
    uid: object = None
    summary: object = None
    location: object = None
    status: object = None
    dtstart: object = None
    dtend: object = None
    duration: object = None
    explicit_duration: bool = False
    rrule: object = None
    rdates: list = field(default_factory=list)
    exdates: list = field(default_factory=list)
    recurrence_id: object = None
    range: object = None


_SINGLE_PROPS = {'UID', 'SUMMARY', 'LOCATION', 'STATUS', 'DTSTART', 'DTEND', 'DURATION', 'RRULE', 'RECURRENCE-ID'}


def _parse_caltime_list(value, params, prop):
    vtype = (params.get('VALUE') or [''])[0].strip().upper()
    if vtype == 'PERIOD':
        raise SeriesProblem('unsupported-rdate-period', prop)
    out = []
    for chunk in value.split(','):
        chunk = chunk.strip()
        if chunk:
            out.append(parse_caltime(chunk, params))
    return out


def component_from_raw(raw):
    """把原始属性表解释成组件；取值与结构问题转成 SeriesProblem。"""
    comp = EventComponent(index=raw.index)
    seen = set()
    for name, params, value in raw.props:
        if name in _SINGLE_PROPS:
            if name in seen:
                raise SeriesProblem('duplicate-property', '%s 组件#%d' % (name, raw.index))
            seen.add(name)
        try:
            if name == 'UID':
                comp.uid = unescape_text(value).strip() or None
            elif name == 'SUMMARY':
                comp.summary = unescape_text(value)
            elif name == 'LOCATION':
                comp.location = unescape_text(value)
            elif name == 'STATUS':
                status = value.strip().upper()
                if status:
                    comp.status = status
            elif name == 'DTSTART':
                comp.dtstart = parse_caltime(value, params)
            elif name == 'DTEND':
                comp.dtend = parse_caltime(value, params)
            elif name == 'DURATION':
                comp.duration = parse_duration(value)
            elif name == 'RRULE':
                comp.rrule = rrule_mod.parse_rrule(value)
            elif name == 'RDATE':
                comp.rdates.extend(_parse_caltime_list(value, params, 'RDATE'))
            elif name == 'EXDATE':
                comp.exdates.extend(_parse_caltime_list(value, params, 'EXDATE'))
            elif name == 'RECURRENCE-ID':
                comp.recurrence_id = parse_caltime(value, params)
                rng = (params.get('RANGE') or ['THISANDPRIOR'])[0].strip().upper()
                if rng == 'THISANDFUTURE':
                    comp.range = 'THISANDFUTURE'
                elif rng != 'THISANDPRIOR':
                    raise SeriesProblem('unsupported-range', rng)
        except ParseError as exc:
            raise SeriesProblem('bad-value', '%s: %s' % (name, exc)) from exc
        except rrule_mod.UnsupportedRule as exc:
            raise SeriesProblem('unsupported-rule', str(exc)) from exc
    if comp.dtstart is not None:
        if comp.dtend is not None:
            if comp.dtend.kind != comp.dtstart.kind:
                raise SeriesProblem('dtend-type-mismatch', '组件#%d' % raw.index)
            duration = comp.dtend.wall_dt() - comp.dtstart.wall_dt()
            if duration < timedelta(0):
                raise SeriesProblem('negative-duration', '组件#%d' % raw.index)
            comp.duration = duration
            comp.explicit_duration = True
        elif comp.duration is not None:
            if comp.duration < timedelta(0):
                raise SeriesProblem('negative-duration', '组件#%d' % raw.index)
            comp.explicit_duration = True
        else:
            comp.duration = timedelta(days=1) if comp.dtstart.kind == 'date' else timedelta(0)
        for rdate in comp.rdates:
            if rdate.kind != comp.dtstart.kind:
                raise SeriesProblem('rdate-type-mismatch', '组件#%d' % raw.index)
    return comp


def _wall_dt(wall):
    return wall if isinstance(wall, datetime) else datetime.combine(wall, time.min)


def _occ_key(kind, wall):
    if kind == 'date' and isinstance(wall, datetime):
        return (kind, wall.date())
    return (kind, wall)


def _expand_walls(comp, tz, env):
    """在墙钟空间有界展开一个组件的 RRULE；UNTIL 先换算到该系列的墙钟。"""
    start = comp.dtstart.wall_dt()
    if comp.rrule is None:
        return [start]
    rule = comp.rrule
    until = rule.until
    if isinstance(until, datetime) and until.tzinfo is not None:
        until = from_utc(tz, until.astimezone(timezone.utc).replace(tzinfo=None))
    elif isinstance(until, date) and not isinstance(until, datetime):
        until = datetime.combine(until, time.max)
    if until is not rule.until:
        rule = replace(rule, until=until)
    lo, hi = env.wall_bounds(comp.duration)
    try:
        return rrule_mod.expand(rule, start, lo, hi)
    except rrule_mod.ExpansionOverflow as exc:
        raise SeriesProblem('expansion-overflow', str(exc)) from exc


def _make_record(uid, rid_key, kind, wall, duration, tz, tz_label, status,
                 summary, location, original, role, component_index, env):
    """换算 UTC、按窗口归类；与窗口无关的实例返回 None。

    取消记录只在原发生落在窗内时保留（window_role=shadow）；被例外改期的实例
    即使新时间移出窗口，只要原发生在窗内也以 shadow 保留，变化不丢失。
    """
    start = _wall_dt(wall)
    end = start + duration
    start_utc = to_utc(tz, start)
    end_utc = to_utc(tz, end)
    in_window = env.overlaps(start_utc, end_utc)
    original_start_utc = None
    original_in_window = False
    if original is not None:
        o_wall, o_tz, o_duration = original
        o_start = _wall_dt(o_wall)
        original_start_utc = to_utc(o_tz, o_start)
        original_in_window = env.overlaps(original_start_utc, to_utc(o_tz, o_start + o_duration))
    if status == 'CANCELLED':
        if not in_window:
            return None
        in_window = False
        window_role = 'shadow'
    elif in_window:
        window_role = 'effective'
    elif original_in_window:
        window_role = 'shadow'
    else:
        return None
    return {
        'key': {'uid': uid, 'recurrence_id': rid_key},
        'summary': summary,
        'location': location,
        'status': (status or 'CONFIRMED').lower(),
        'all_day': kind == 'date',
        'tzid': tz_label,
        'start_utc': iso_z(start_utc),
        'end_utc': iso_z(end_utc),
        'original_start_utc': iso_z(original_start_utc) if original_start_utc is not None else None,
        'in_window': in_window,
        'window_role': window_role,
        'provenance': {'component': component_index, 'role': role},
    }


def expand_series(uid, comps, env):
    """展开一个 UID 系列，返回窗口相关的实例记录；冲突与超范围抛 SeriesProblem。"""
    masters = [c for c in comps if c.recurrence_id is None]
    if len(masters) > 1:
        raise SeriesProblem('duplicate-master', uid)
    exceptions = [c for c in comps if c.recurrence_id is not None]
    seen = set()
    for exc in exceptions:
        key = exc.recurrence_id.key()
        if key in seen:
            raise SeriesProblem('duplicate-exception', key)
        seen.add(key)
    futures = [e for e in exceptions if e.range == 'THISANDFUTURE']
    if len(futures) > 1:
        raise SeriesProblem('multiple-thisandfuture', uid)
    singles = [e for e in exceptions if e.range != 'THISANDFUTURE']
    master = masters[0] if masters else None
    if master is not None and master.dtstart is None:
        raise SeriesProblem('missing-dtstart', uid)

    records = []
    occ = {}
    master_tz = master_label = None
    if master is not None:
        master_tz, master_label = env.resolve(master.dtstart)
        for wall in _expand_walls(master, master_tz, env):
            occ[_occ_key(master.dtstart.kind, wall)] = ('rrule', master.index, master_tz, master_label)
        for rdate in master.rdates:
            rdate_tz, rdate_label = env.resolve(rdate)
            occ[(rdate.kind, rdate.wall)] = ('rdate', master.index, rdate_tz, rdate_label)
        for exdate in master.exdates:
            occ.pop((exdate.kind, exdate.wall), None)

    spawned = {}
    future = futures[0] if futures else None
    future_tz = future_label = None
    if future is not None:
        rid = future.recurrence_id
        removed = sorted(((k, occ[k]) for k in occ
                          if k[0] == rid.kind and _wall_dt(k[1]) >= _wall_dt(rid.wall)),
                         key=lambda item: _wall_dt(item[0][1]))
        if future.status == 'CANCELLED':
            for (kind, wall), (role, idx, tz, label) in removed:
                rec = _make_record(uid, CalTime(kind, wall, master.dtstart.tzid).key(), kind, wall,
                                   master.duration, tz, label, 'CANCELLED',
                                   master.summary, master.location, None, 'thisandfuture', future.index, env)
                if rec:
                    records.append(rec)
            for key, _ in removed:
                del occ[key]
        else:
            if future.dtstart is None:
                raise SeriesProblem('missing-dtstart', 'THISANDFUTURE ' + uid)
            future_tz, future_label = env.resolve(future.dtstart)
            for key, _ in removed:
                del occ[key]
            for wall in _expand_walls(future, future_tz, env):
                spawned[_occ_key(future.dtstart.kind, wall)] = ('thisandfuture', future.index,
                                                                future_tz, future_label)
            for rdate in future.rdates:
                rdate_tz, rdate_label = env.resolve(rdate)
                spawned[(rdate.kind, rdate.wall)] = ('thisandfuture', future.index, rdate_tz, rdate_label)
            for exdate in future.exdates:
                spawned.pop((exdate.kind, exdate.wall), None)
            if removed and master is not None:
                rec = _make_record(uid, rid.key(), future.dtstart.kind, future.dtstart.wall,
                                   future.duration, future_tz, future_label,
                                   future.status or 'CONFIRMED',
                                   future.summary if future.summary is not None else master.summary,
                                   future.location if future.location is not None else master.location,
                                   (rid.wall, master_tz, master.duration), 'thisandfuture', future.index, env)
                if rec and rec['window_role'] == 'shadow':
                    records.append(rec)

    for exc in singles:
        rid = exc.recurrence_id
        key = (rid.kind, rid.wall)
        in_master = key in occ
        in_spawned = key in spawned
        if exc.status == 'CANCELLED':
            if in_master or in_spawned:
                source = occ if in_master else spawned
                _, _, tz, label = source[key]
                base = master if in_master else future
                duration, wall = base.duration, rid.wall
                summary, location = base.summary, base.location
            else:
                anchor = exc.dtstart if exc.dtstart is not None else rid
                tz, label = env.resolve(anchor)
                duration = exc.duration if exc.dtstart is not None else (
                    master.duration if master is not None else timedelta(0))
                summary, location, wall = exc.summary, exc.location, anchor.wall
            rec = _make_record(uid, rid.key(), rid.kind, wall, duration, tz, label, 'CANCELLED',
                               summary, location, None, 'exception', exc.index, env)
            if rec:
                records.append(rec)
            if in_master:
                del occ[key]
            elif in_spawned:
                del spawned[key]
            continue
        if exc.dtstart is None:
            raise SeriesProblem('missing-dtstart', 'RECURRENCE-ID ' + rid.key())
        exc_tz, exc_label = env.resolve(exc.dtstart)
        duration = exc.duration if exc.explicit_duration else (
            master.duration if master is not None else exc.duration)
        if in_master:
            original = (rid.wall, occ[key][2], master.duration)
            del occ[key]
        elif in_spawned:
            original = (rid.wall, spawned[key][2], future.duration)
            del spawned[key]
        else:
            original = None
        rec = _make_record(uid, rid.key(), exc.dtstart.kind, exc.dtstart.wall, duration, exc_tz, exc_label,
                           exc.status or (master.status if master is not None else None) or 'CONFIRMED',
                           exc.summary if exc.summary is not None else (master.summary if master else None),
                           exc.location if exc.location is not None else (master.location if master else None),
                           original, 'exception', exc.index, env)
        if rec:
            records.append(rec)

    if master is not None:
        for (kind, wall), (role, idx, tz, label) in occ.items():
            status = 'CANCELLED' if master.status == 'CANCELLED' else (master.status or 'CONFIRMED')
            rec = _make_record(uid, CalTime(kind, wall, master.dtstart.tzid).key(), kind, wall,
                               master.duration, tz, label, status,
                               master.summary, master.location, None, role, idx, env)
            if rec:
                records.append(rec)
    if future is not None and future.status != 'CANCELLED':
        for (kind, wall), (role, idx, tz, label) in spawned.items():
            rec = _make_record(uid, CalTime(kind, wall, future.dtstart.tzid).key(), kind, wall,
                               future.duration, tz, label, future.status or 'CONFIRMED',
                               future.summary if future.summary is not None else (master.summary if master else None),
                               future.location if future.location is not None else (master.location if master else None),
                               None, role, idx, env)
            if rec:
                records.append(rec)
    return records
