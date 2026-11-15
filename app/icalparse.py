"""iCalendar 内容行、类型化取值与 VTIMEZONE 解释；只读来源，不做任何写回。"""
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo

from . import rrule as rrule_mod


class ParseError(ValueError):
    """内容无法按 iCalendar 解释。"""


@dataclass(frozen=True)
class CalTime:
    """日期/日期时间取值：种类、本地墙钟、时区标注（'UTC'/来源TZID/None 浮动）。"""
    kind: str
    wall: object
    tzid: object = None

    def key(self):
        if self.kind == 'date':
            return 'D:' + self.wall.isoformat()
        if self.tzid == 'UTC':
            return 'Z:' + self.wall.isoformat() + 'Z'
        if self.tzid:
            return 'T:' + self.tzid + ':' + self.wall.isoformat()
        return 'L:' + self.wall.isoformat()

    def wall_dt(self):
        if self.kind == 'date':
            return datetime.combine(self.wall, time.min)
        return self.wall


def iso_z(moment):
    """朴素 UTC datetime 的交付形式。"""
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def unfold_lines(text):
    lines = []
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if raw[:1] in (' ', '\t') and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return [line for line in lines if line != '']


def parse_line(line):
    """拆出属性名、参数表与取值；参数值允许带引号。"""
    in_quote = False
    split_at = None
    for i, char in enumerate(line):
        if char == '"':
            in_quote = not in_quote
        elif char == ':' and not in_quote:
            split_at = i
            break
    if split_at is None:
        raise ParseError('内容行缺少取值冒号')
    head, value = line[:split_at], line[split_at + 1:]
    parts = []
    current = ''
    in_quote = False
    for char in head:
        if char == '"':
            in_quote = not in_quote
            current += char
        elif char == ';' and not in_quote:
            parts.append(current)
            current = ''
        else:
            current += char
    parts.append(current)
    name = parts[0].strip().upper()
    if not name:
        raise ParseError('内容行缺少属性名')
    params = {}
    for part in parts[1:]:
        if '=' not in part:
            raise ParseError('参数缺少等号: ' + part)
        key, val = part.split('=', 1)
        vals = [item.strip().strip('"') for item in val.split(',')]
        params.setdefault(key.strip().upper(), []).extend(vals)
    return name, params, value


_ESCAPES = {'n': '\n', 'N': '\n', '\\': '\\', ',': ',', ';': ';'}


def unescape_text(value):
    out = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == '\\' and i + 1 < len(value):
            out.append(_ESCAPES.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            out.append(char)
            i += 1
    return ''.join(out)


_DATE_RE = re.compile(r'^\d{8}$')
_DATETIME_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$')


def parse_caltime(value, params):
    """把属性取值解释成 CalTime；VALUE=PERIOD 等超出口径的形式由调用方另行拒绝。"""
    vtype = (params.get('VALUE') or [''])[0].strip().upper()
    tzid = (params.get('TZID') or [None])[0]
    text = value.strip()
    try:
        if vtype == 'DATE' or (not vtype and _DATE_RE.match(text)):
            return CalTime('date', date(int(text[:4]), int(text[4:6]), int(text[6:8])), None)
        match = _DATETIME_RE.match(text)
        if vtype in ('', 'DATE-TIME') and match:
            wall = datetime(*[int(match.group(i)) for i in range(1, 7)])
            if match.group(7) == 'Z':
                return CalTime('datetime', wall, 'UTC')
            return CalTime('datetime', wall, tzid or None)
    except ValueError:
        pass
    raise ParseError('日期时间取值无效: ' + text)


_DURATION_RE = re.compile(r'^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$')


def parse_duration(value):
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ParseError('时长取值无效: ' + value)
    sign = -1 if match.group(1) == '-' else 1
    weeks, days, hours, minutes, seconds = (int(match.group(i) or 0) for i in range(2, 7))
    return sign * timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)


_OFFSET_RE = re.compile(r'^([+-])(\d{2})(\d{2})(\d{2})?$')


def _parse_utc_offset(value):
    match = _OFFSET_RE.match(value.strip())
    if not match:
        raise ParseError('UTC 偏移无效: ' + value)
    sign = -1 if match.group(1) == '-' else 1
    return sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3)),
                            seconds=int(match.group(4) or 0))


@dataclass(frozen=True)
class _Observance:
    start: object
    offset_from: object
    offset_to: object
    rrule: object
    rdates: tuple


class VTimezone(tzinfo):
    """来源自带 VTIMEZONE 的时区实现：按观测切换表换算，视界外沿用最后的偏移。"""

    def __init__(self, tzid, observances, horizon):
        self.tzid = tzid
        transitions = []
        for obs in observances:
            onsets = list(obs.rdates)
            if obs.rrule is not None:
                onsets.extend(rrule_mod.expand(obs.rrule, obs.start, obs.start, horizon))
            else:
                onsets.append(obs.start)
            for wall in onsets:
                transitions.append((wall - obs.offset_from, wall, obs.offset_to, obs.offset_from))
        transitions.sort()
        deduped = []
        for item in transitions:
            if deduped and item[0] == deduped[-1][0] and item[2] == deduped[-1][2]:
                continue
            deduped.append(item)
        self._trans = deduped
        self._utc_onsets = [item[0] for item in deduped]
        self._wall_onsets = [item[1] for item in deduped]
        self._initial = deduped[0][3] if deduped else timedelta(0)

    def offset_at_utc(self, utc_naive):
        i = bisect_right(self._utc_onsets, utc_naive) - 1
        return self._trans[i][2] if i >= 0 else self._initial

    def to_utc(self, wall, fold=0):
        """本地墙钟 → 朴素 UTC；空洞按切换前偏移（等价向前平移），重叠 fold=0 取先发生的时刻。"""
        i = bisect_right(self._wall_onsets, wall) - 1
        candidates = {self._trans[i][2] if i >= 0 else self._initial}
        if 0 <= i + 1 < len(self._trans):
            candidates.add(self._trans[i + 1][2])
        valid = []
        for offset in candidates:
            utc = wall - offset
            if self.offset_at_utc(utc) == offset:
                valid.append(utc)
        if not valid:
            before = self._trans[i][3] if i >= 0 else self._initial
            return wall - before
        return min(valid) if fold == 0 else max(valid)

    def from_utc(self, utc_naive):
        return utc_naive + self.offset_at_utc(utc_naive)

    def utcoffset(self, dt):
        if dt is None:
            return None
        return self.offset_at_utc(self.to_utc(dt.replace(tzinfo=None), getattr(dt, 'fold', 0)))

    def tzname(self, dt):
        return self.tzid

    def dst(self, dt):
        return None

    def __repr__(self):
        return 'VTimezone(%r)' % self.tzid


def to_utc(tz, wall, fold=0):
    """任意支持的时区对象：本地墙钟 → 朴素 UTC。"""
    if isinstance(tz, VTimezone):
        return tz.to_utc(wall, fold)
    return wall.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc).replace(tzinfo=None)


def from_utc(tz, utc_naive):
    if isinstance(tz, VTimezone):
        return tz.from_utc(utc_naive)
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)


def offset_at(tz, utc_naive):
    if isinstance(tz, VTimezone):
        return tz.offset_at_utc(utc_naive)
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(tz).utcoffset()


@dataclass
class RawEvent:
    index: int
    props: list


@dataclass
class ParsedCalendar:
    events: list
    vtimezones: dict
    errors: list


def _build_vtimezone(raw, horizon):
    tzid = raw['tzid']
    if not tzid:
        raise ParseError('VTIMEZONE 缺少 TZID')
    observances = []
    for obs in raw['obs']:
        props = {}
        rdates = []
        for name, params, value in obs['props']:
            if name in ('DTSTART', 'TZOFFSETFROM', 'TZOFFSETTO', 'RRULE'):
                if name in props:
                    raise ParseError('观测属性重复: ' + name)
                props[name] = value
            elif name == 'RDATE':
                for chunk in value.split(','):
                    moment = parse_caltime(chunk.strip(), params)
                    if moment.kind != 'datetime' or moment.tzid is not None:
                        raise ParseError('VTIMEZONE RDATE 须为本地日期时间')
                    rdates.append(moment.wall)
        missing = {'DTSTART', 'TZOFFSETFROM', 'TZOFFSETTO'} - set(props)
        if missing:
            raise ParseError('观测缺少属性: ' + ','.join(sorted(missing)))
        start = parse_caltime(props['DTSTART'], {})
        if start.kind != 'datetime' or start.tzid is not None:
            raise ParseError('VTIMEZONE DTSTART 须为本地日期时间')
        rule = None
        if 'RRULE' in props:
            try:
                rule = rrule_mod.parse_rrule(props['RRULE'], allowed_freqs=('YEARLY',))
            except rrule_mod.UnsupportedRule as exc:
                raise ParseError('VTIMEZONE 规则不支持: %s' % exc)
            if isinstance(rule.until, datetime) and rule.until.tzinfo is not None:
                raise ParseError('VTIMEZONE UNTIL 须为本地时间')
            if isinstance(rule.until, date) and not isinstance(rule.until, datetime):
                rule = rrule_mod.RRule(**{**rule.__dict__, 'until': datetime.combine(rule.until, time.max)})
        observances.append(_Observance(start.wall, _parse_utc_offset(props['TZOFFSETFROM']),
                                       _parse_utc_offset(props['TZOFFSETTO']), rule, tuple(rdates)))
    if not observances:
        raise ParseError('VTIMEZONE 缺少观测')
    return VTimezone(tzid, observances, horizon)


def parse_calendar(data, horizon):
    """把原始实体字节解析成组件序列；结构问题记入 errors，整体不可识别时抛 ParseError。"""
    text = data.decode('utf-8')
    events = []
    vtimezones = {}
    errors = []
    stack = []
    cur_event = None
    cur_vtz = None
    cur_obs = None
    saw_calendar = False
    for line in unfold_lines(text):
        try:
            name, params, value = parse_line(line)
        except ParseError as exc:
            errors.append('bad-line: %s' % exc)
            continue
        if name == 'BEGIN':
            part = value.strip().upper()
            stack.append(part)
            if part == 'VCALENDAR':
                saw_calendar = True
            elif part == 'VEVENT':
                if cur_event is not None:
                    errors.append('nested-vevent')
                cur_event = []
            elif part == 'VTIMEZONE':
                cur_vtz = {'tzid': None, 'obs': []}
            elif part in ('STANDARD', 'DAYLIGHT') and cur_vtz is not None:
                cur_obs = {'kind': part, 'props': []}
            continue
        if name == 'END':
            part = value.strip().upper()
            if part == 'VEVENT' and cur_event is not None:
                events.append(RawEvent(len(events), cur_event))
                cur_event = None
            elif part in ('STANDARD', 'DAYLIGHT') and cur_obs is not None:
                cur_vtz['obs'].append(cur_obs)
                cur_obs = None
            elif part == 'VTIMEZONE' and cur_vtz is not None:
                try:
                    tz = _build_vtimezone(cur_vtz, horizon)
                    vtimezones[tz.tzid] = tz
                except ParseError as exc:
                    errors.append('vtimezone: %s' % exc)
                cur_vtz = None
            if stack and stack[-1] == part:
                stack.pop()
            continue
        if cur_obs is not None:
            cur_obs['props'].append((name, params, value))
        elif cur_vtz is not None:
            if name == 'TZID':
                cur_vtz['tzid'] = value.strip()
        elif cur_event is not None:
            cur_event.append((name, params, value))
    if not saw_calendar:
        raise ParseError('响应不是 VCALENDAR 日历')
    if cur_event is not None or cur_vtz is not None:
        errors.append('unclosed-component')
    return ParsedCalendar(events, vtimezones, errors)
