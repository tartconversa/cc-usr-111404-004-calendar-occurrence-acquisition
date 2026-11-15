"""按 UTC 半开窗领取实例、标注未能展开的系列，并按来源版本去重与比对。

HTTP 取到实体不等于实例采集完整：只有解析无结构错误且没有未能展开的系列时
结果才标记 complete 并写入状态；相同版本重复取得时不重复交付。
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .icalparse import iso_z, offset_at, parse_calendar
from .series import SeriesProblem, component_from_raw, expand_series

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


class StateStore:
    """按来源与采集窗持久化已交付版本；path 为 None 时仅存在于内存。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.data = {'format': 1, 'sources': {}}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if self.data.get('format') != 1:
                raise ValueError('状态文件格式不支持')

    def get(self, source_id, window_key):
        return self.data['sources'].get(source_id, {}).get(window_key)

    def put(self, source_id, window_key, entry):
        self.data['sources'].setdefault(source_id, {})[window_key] = entry
        if self.path:
            tmp = self.path.with_name(self.path.name + '.tmp')
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding='utf-8')
            tmp.replace(self.path)


class _Resolver:
    """TZID 解析：来源 VTIMEZONE 或冻结 IANA 映射；二者都有且不一致时报告冲突。"""

    def __init__(self, vtimezones, tz_map, w0, w1):
        self.vtimezones = vtimezones
        self.tz_map = tz_map
        years = range(w0.year - 1, w1.year + 2)
        self.samples = [w0, w1] + [datetime(y, 1, 1) for y in years] + [datetime(y, 7, 1) for y in years]

    def _mapped(self, tzid):
        if self.tz_map is not None:
            return self.tz_map.get(tzid)
        if ZoneInfo is None:
            return None
        try:
            return ZoneInfo(tzid)
        except Exception:
            return None

    def resolve(self, tzid):
        from_source = self.vtimezones.get(tzid)
        mapped = self._mapped(tzid)
        if from_source is not None and mapped is not None:
            for moment in self.samples:
                if offset_at(from_source, moment) != offset_at(mapped, moment):
                    raise SeriesProblem('tz-conflict', tzid)
        tz = from_source if from_source is not None else mapped
        if tz is None:
            raise SeriesProblem('unknown-tz', tzid)
        return tz


class _Env:
    """单来源单次采集的换算环境：浮动时区、墙钟边界与窗口相交判定。"""

    def __init__(self, resolver, floating_tz, w0, w1):
        self.resolver = resolver
        self.w0 = w0
        self.w1 = w1
        self._floating_spec = floating_tz
        self._floating = None
        self._floating_label = None
        self._floating_done = False

    def _floating_tz(self):
        if not self._floating_done:
            self._floating_done = True
            spec = self._floating_spec
            if isinstance(spec, str):
                self._floating = self.resolver.resolve(spec)
                self._floating_label = spec
            elif spec is not None:
                self._floating = spec
                self._floating_label = getattr(spec, 'key', None) or str(spec)
        return self._floating, self._floating_label

    def resolve(self, caltime):
        if caltime.kind == 'date' or caltime.tzid is None:
            tz, label = self._floating_tz()
            if tz is None:
                raise SeriesProblem('no-floating-tz', '来源未预登记浮动时区')
            return tz, 'floating:' + label
        if caltime.tzid == 'UTC':
            return timezone.utc, 'UTC'
        return self.resolver.resolve(caltime.tzid), caltime.tzid

    def wall_bounds(self, duration):
        """生成界：窗口换算墙钟后外放 2 天（覆盖一切时区偏移）并加上事件时长。"""
        return (self.w0 - timedelta(days=2) - duration, self.w1 + timedelta(days=2))

    def overlaps(self, start, end):
        if end > start:
            return start < self.w1 and end > self.w0
        return self.w0 <= start < self.w1


def _as_utc(value, name):
    if isinstance(value, str):
        text = value.strip()
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(name + ' 时刻格式无效')
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(name + ' 须为带时区的时刻')
    return value.astimezone(timezone.utc).replace(tzinfo=None)


_DIGEST_FIELDS = ('summary', 'location', 'status', 'all_day', 'tzid',
                  'start_utc', 'end_utc', 'original_start_utc')


def _digest(payload):
    text = json.dumps({k: payload[k] for k in _DIGEST_FIELDS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _key_string(key):
    return json.dumps([key['uid'], key['recurrence_id']], ensure_ascii=False)


def _diff(old_map, new_map):
    added = [new_map[k]['payload'] for k in sorted(new_map.keys() - old_map.keys())]
    revoked = [old_map[k]['payload'] for k in sorted(old_map.keys() - new_map.keys())]
    modified = [{'key': new_map[k]['payload']['key'],
                 'previous': old_map[k]['payload'],
                 'current': new_map[k]['payload']}
                for k in sorted(new_map.keys() & old_map.keys())
                if new_map[k]['digest'] != old_map[k]['digest']]
    return {'added': added, 'revoked': revoked, 'modified': modified}


def collect_response(response, *, window_start, window_end, floating_tz=None, tz_map=None, state=None):
    """对一份来源响应做实例采集；state 为 StateStore 时按版本去重并给出变化。"""
    w0 = _as_utc(window_start, 'window_start')
    w1 = _as_utc(window_end, 'window_end')
    if not w0 < w1:
        raise ValueError('采集窗必须为起始小于终止的半开区间')
    version = response.sha256
    etag = response.header('ETag')
    base = {'source_id': response.source_id,
            'http_status': response.status,
            'window': {'start': iso_z(w0), 'end': iso_z(w1)},
            'version': {'response_sha256': version, 'etag': etag}}
    window_key = base['window']['start'] + '/' + base['window']['end']
    cached = state.get(response.source_id, window_key) if state is not None else None
    if cached and cached.get('version_sha256') == version and cached.get('complete'):
        return {**base, 'unchanged': True, 'complete': True}
    try:
        parsed = parse_calendar(response.body, horizon=datetime(w1.year + 2, 1, 1))
    except Exception as exc:
        return {**base, 'unchanged': False, 'complete': False, 'initial': cached is None,
                'errors': ['parse-error: %s' % exc], 'unexpanded': [], 'instances': [],
                'changes': None}
    errors = list(parsed.errors)
    resolver = _Resolver(parsed.vtimezones, tz_map, w0, w1)
    env = _Env(resolver, floating_tz, w0, w1)
    groups = {}
    unexpanded = []
    for raw in parsed.events:
        try:
            comp = component_from_raw(raw)
        except SeriesProblem as problem:
            unexpanded.append({'uid': None, 'reason': problem.reason, 'detail': str(problem)})
            continue
        if not comp.uid:
            unexpanded.append({'uid': None, 'reason': 'missing-uid', 'detail': '组件#%d' % comp.index})
            continue
        groups.setdefault(comp.uid, []).append(comp)
    records = []
    for uid in sorted(groups):
        try:
            records.extend(expand_series(uid, groups[uid], env))
        except SeriesProblem as problem:
            unexpanded.append({'uid': uid, 'reason': problem.reason, 'detail': str(problem)})
    complete = not unexpanded and not errors
    instances = []
    for rec in records:
        rec['key'] = {'source_id': response.source_id, **rec['key']}
        rec['provenance']['response_sha256'] = version
        instances.append(rec)
    instances.sort(key=lambda p: (p['start_utc'], p['key']['uid'], p['key']['recurrence_id']))
    unexpanded.sort(key=lambda u: (u['uid'] or '', u['reason']))
    result = {**base, 'unchanged': False, 'complete': complete, 'errors': errors,
              'unexpanded': unexpanded, 'instances': instances}
    new_map = {_key_string(p['key']): {'digest': _digest(p), 'payload': p} for p in instances}
    if cached is not None and cached.get('instances') is not None:
        result['changes'] = _diff(cached['instances'], new_map)
        result['initial'] = False
    else:
        result['changes'] = None
        result['initial'] = True
    if complete and state is not None:
        state.put(response.source_id, window_key, {
            'version_sha256': version, 'etag': etag, 'complete': True,
            'collected_utc': iso_z(datetime.now(timezone.utc).replace(tzinfo=None)),
            'instances': new_map})
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(prog='python -m app.collect', description='按 UTC 半开窗领取来源实例')
    parser.add_argument('packet', help='来源响应 JSON 路径')
    parser.add_argument('window_start', help='采集窗起点（UTC，ISO 8601）')
    parser.add_argument('window_end', help='采集窗终点（UTC，ISO 8601，半开）')
    parser.add_argument('--floating-tz', help='来源预登记的浮动/全天时区（IANA 名）')
    parser.add_argument('--state', help='交付状态文件路径（缺省仅内存）')
    args = parser.parse_args()
    from .source import from_packet
    packet = json.loads(Path(args.packet).read_text(encoding='utf-8'))
    state = StateStore(args.state) if args.state else StateStore()
    result = collect_response(from_packet(packet), window_start=args.window_start,
                              window_end=args.window_end, floating_tz=args.floating_tz, state=state)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
