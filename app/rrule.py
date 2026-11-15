"""RRULE 解析与围绕查询窗的有界展开；展开在本地墙钟空间进行，不按固定秒数平移。"""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

WEEKDAY_NO = {'MO': 0, 'TU': 1, 'WE': 2, 'TH': 3, 'FR': 4, 'SA': 5, 'SU': 6}
EVENT_FREQS = ('DAILY', 'WEEKLY', 'MONTHLY')
MAX_PERIODS = 200000
MAX_CANDIDATES = 200000


class UnsupportedRule(ValueError):
    """规则超出来源契约的支持范围。"""


class ExpansionOverflow(Exception):
    """展开工作量超过安全上限，调用方应把系列记为未能展开。"""


@dataclass(frozen=True)
class RRule:
    """已解析的重复规则；until 可能是 date、朴素 datetime 或带时区的 UTC datetime。"""
    freq: str
    interval: int = 1
    count: object = None
    until: object = None
    byday: tuple = ()
    bymonthday: tuple = ()
    bymonth: tuple = ()
    bysetpos: tuple = ()
    wkst: int = 0


_KNOWN_PARTS = {'INTERVAL', 'COUNT', 'UNTIL', 'BYDAY', 'BYMONTHDAY', 'BYMONTH', 'BYSETPOS', 'WKST'}
_BYDAY_RE = re.compile(r'^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$')


def _int_list(value, name, lo, hi):
    out = []
    for chunk in value.split(','):
        try:
            number = int(chunk)
        except ValueError:
            raise UnsupportedRule('%s 取值无效: %s' % (name, chunk))
        if not lo <= number <= hi:
            raise UnsupportedRule('%s 超出范围: %d' % (name, number))
        out.append(number)
    return tuple(out)


def _parse_until(value):
    try:
        if re.fullmatch(r'\d{8}', value):
            return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
        match = re.fullmatch(r'(\d{8})T(\d{6})(Z?)', value)
        if not match:
            raise ValueError
        wall = datetime.strptime(value[:15], '%Y%m%dT%H%M%S')
    except ValueError:
        raise UnsupportedRule('UNTIL 取值无效: ' + value)
    return wall.replace(tzinfo=timezone.utc) if match.group(3) == 'Z' else wall


def parse_rrule(value, allowed_freqs=EVENT_FREQS):
    """解析 RRULE；超出支持范围的部分一律抛 UnsupportedRule，不做静默降级。"""
    parts = {}
    for chunk in value.strip().split(';'):
        if not chunk:
            continue
        if '=' not in chunk:
            raise UnsupportedRule('规则部分缺少等号: ' + chunk)
        key, val = chunk.split('=', 1)
        key = key.strip().upper()
        if key in parts:
            raise UnsupportedRule('规则部分重复: ' + key)
        parts[key] = val.strip()
    freq = parts.pop('FREQ', '').upper()
    if freq not in allowed_freqs:
        raise UnsupportedRule('FREQ 超出支持范围: ' + (freq or '缺失'))
    unknown = sorted(set(parts) - _KNOWN_PARTS)
    if unknown:
        raise UnsupportedRule('不支持的规则部分: ' + ','.join(unknown))
    interval = count = None
    if 'INTERVAL' in parts:
        try:
            interval = int(parts['INTERVAL'])
        except ValueError:
            raise UnsupportedRule('INTERVAL 取值无效')
        if interval < 1:
            raise UnsupportedRule('INTERVAL 必须为正')
    if 'COUNT' in parts:
        try:
            count = int(parts['COUNT'])
        except ValueError:
            raise UnsupportedRule('COUNT 取值无效')
        if count < 1:
            raise UnsupportedRule('COUNT 必须为正')
    until = _parse_until(parts['UNTIL']) if 'UNTIL' in parts else None
    byday = []
    if 'BYDAY' in parts:
        for chunk in parts['BYDAY'].split(','):
            match = _BYDAY_RE.match(chunk.strip())
            if not match:
                raise UnsupportedRule('BYDAY 取值无效: ' + chunk)
            ordinal = int(match.group(1)) if match.group(1) else None
            if ordinal == 0:
                raise UnsupportedRule('BYDAY 序数不能为 0')
            byday.append((ordinal, WEEKDAY_NO[match.group(2)]))
    bymonthday = _int_list(parts['BYMONTHDAY'], 'BYMONTHDAY', -31, 31) if 'BYMONTHDAY' in parts else ()
    if 0 in bymonthday:
        raise UnsupportedRule('BYMONTHDAY 不能为 0')
    bymonth = _int_list(parts['BYMONTH'], 'BYMONTH', 1, 12) if 'BYMONTH' in parts else ()
    bysetpos = _int_list(parts['BYSETPOS'], 'BYSETPOS', -366, 366) if 'BYSETPOS' in parts else ()
    if 0 in bysetpos:
        raise UnsupportedRule('BYSETPOS 不能为 0')
    if any(ordinal is not None for ordinal, _ in byday) and (freq not in ('MONTHLY', 'YEARLY') or bymonthday):
        raise UnsupportedRule('BYDAY 序数仅支持 MONTHLY/YEARLY 且无 BYMONTHDAY')
    wkst = 0
    if 'WKST' in parts:
        if parts['WKST'].upper() not in WEEKDAY_NO:
            raise UnsupportedRule('WKST 取值无效')
        wkst = WEEKDAY_NO[parts['WKST'].upper()]
    return RRule(freq, interval or 1, count, until, tuple(byday), bymonthday, bymonth, bysetpos, wkst)


def _month_len(year, month):
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (nxt - date(year, month, 1)).days


def _week0(dtstart, wkst):
    day = dtstart.date() if isinstance(dtstart, datetime) else dtstart
    return day - timedelta(days=(day.weekday() - wkst) % 7)


def _advance(rrule, dtstart, k):
    day0 = dtstart.date() if isinstance(dtstart, datetime) else dtstart
    if rrule.freq == 'DAILY':
        return day0 + timedelta(days=k * rrule.interval)
    if rrule.freq == 'WEEKLY':
        return _week0(dtstart, rrule.wkst) + timedelta(weeks=k * rrule.interval)
    if rrule.freq == 'MONTHLY':
        total = day0.year * 12 + (day0.month - 1) + k * rrule.interval
        return date(total // 12, total % 12 + 1, 1)
    return date(day0.year + k * rrule.interval, 1, 1)


def _start_index(rrule, dtstart, lo):
    """估计可快进的周期序号；带 COUNT 的规则由调用方禁用快进。"""
    day0 = dtstart.date() if isinstance(dtstart, datetime) else dtstart
    day_lo = lo.date() if isinstance(lo, datetime) else lo
    if rrule.freq == 'DAILY':
        n = (day_lo - day0).days // rrule.interval
    elif rrule.freq == 'WEEKLY':
        n = (day_lo - _week0(dtstart, rrule.wkst)).days // (7 * rrule.interval)
    elif rrule.freq == 'MONTHLY':
        n = ((day_lo.year * 12 + day_lo.month - 1) - (day0.year * 12 + day0.month - 1)) // rrule.interval
    else:
        n = day_lo.year - day0.year
    return max(0, n - 1)


def _monthday_days(year, month, spec):
    length = _month_len(year, month)
    out = []
    for item in spec:
        day = item if item > 0 else length + 1 + item
        if 1 <= day <= length:
            out.append(date(year, month, day))
    return out


def _byday_month_days(year, month, byday):
    out = []
    for ordinal, weekday in byday:
        days = [date(year, month, d) for d in range(1, _month_len(year, month) + 1)
                if date(year, month, d).weekday() == weekday]
        if ordinal is None:
            out.extend(days)
        elif 0 < ordinal <= len(days):
            out.append(days[ordinal - 1])
        elif ordinal < 0 and len(days) + ordinal >= 0:
            out.append(days[ordinal])
    return out


def _limit_byday(days, byday):
    if not byday:
        return days
    wanted = {weekday for _, weekday in byday}
    return [d for d in days if d.weekday() in wanted]


def _limit_bymonthday(days, spec):
    if not spec:
        return days
    out = []
    for d in days:
        length = _month_len(d.year, d.month)
        if any(s == d.day or (s < 0 and length + 1 + s == d.day) for s in spec):
            out.append(d)
    return out


def _candidate_days(rrule, anchor, dtstart):
    if rrule.freq == 'DAILY':
        days = [anchor]
        if rrule.bymonth:
            days = [d for d in days if d.month in rrule.bymonth]
        return _limit_byday(_limit_bymonthday(days, rrule.bymonthday), rrule.byday)
    if rrule.freq == 'WEEKLY':
        weekdays = [weekday for _, weekday in rrule.byday] if rrule.byday else [dtstart.weekday()]
        days = sorted({anchor + timedelta(days=(wd - rrule.wkst) % 7) for wd in weekdays})
        if rrule.bymonth:
            days = [d for d in days if d.month in rrule.bymonth]
        return _limit_bymonthday(days, rrule.bymonthday)
    if rrule.freq == 'MONTHLY':
        if rrule.bymonth and anchor.month not in rrule.bymonth:
            return []
        if rrule.bymonthday:
            return _limit_byday(_monthday_days(anchor.year, anchor.month, rrule.bymonthday), rrule.byday)
        if rrule.byday:
            return _byday_month_days(anchor.year, anchor.month, rrule.byday)
        return _monthday_days(anchor.year, anchor.month, (dtstart.day,))
    days = []
    for month in (rrule.bymonth or (dtstart.month,)):
        if rrule.bymonthday:
            days.extend(_monthday_days(anchor.year, month, rrule.bymonthday))
        elif rrule.byday:
            days.extend(_byday_month_days(anchor.year, month, rrule.byday))
        else:
            days.extend(_monthday_days(anchor.year, month, (dtstart.day,)))
    return sorted(days)


def expand(rrule, dtstart, lo, hi, max_periods=MAX_PERIODS, max_candidates=MAX_CANDIDATES):
    """产生 [lo,hi) 内的墙钟发生时刻（朴素 datetime）；COUNT/UNTIL 从 DTSTART 起算。

    dtstart/lo/hi 均为朴素 datetime；调用方负责把 UNTIL 换算到同一墙钟空间。
    """
    if hi <= lo:
        return []
    if isinstance(rrule.until, datetime) and rrule.until.tzinfo is not None:
        raise UnsupportedRule('UNTIL 未换算到本地墙钟')
    until = rrule.until
    if isinstance(until, date) and not isinstance(until, datetime):
        until = datetime.combine(until, time.max)
    moment = dtstart.time() if isinstance(dtstart, datetime) else time.min
    out = []
    seen = 0
    count_left = rrule.count
    k = 0 if rrule.count is not None else _start_index(rrule, dtstart, lo)
    for _ in range(max_periods):
        anchor = _advance(rrule, dtstart, k)
        if datetime.combine(anchor, time.min) >= hi:
            break
        days = _candidate_days(rrule, anchor, dtstart)
        candidates = sorted(datetime.combine(d, moment) for d in days)
        if rrule.bysetpos:
            picked = []
            size = len(candidates)
            for pos in rrule.bysetpos:
                i = pos - 1 if pos > 0 else size + pos
                if 0 <= i < size:
                    picked.append(candidates[i])
            candidates = sorted(set(picked))
        for candidate in candidates:
            if candidate < dtstart:
                continue
            if until is not None and candidate > until:
                return out
            seen += 1
            if seen > max_candidates:
                raise ExpansionOverflow('候选数量超过上限')
            if count_left is not None:
                count_left -= 1
                if count_left < 0:
                    return out
            if lo <= candidate < hi:
                out.append(candidate)
        k += 1
    else:
        raise ExpansionOverflow('周期数量超过上限')
    return out
