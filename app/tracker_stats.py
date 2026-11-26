"""Per-tracker, per-day statistics of the simulated upload (v1.31).

Fed every second with the measured JOAL rates (bytes/s per torrent); kept in memory and
saved to /data/tracker-stats.json with the upload history (every 30 s), 90 days kept.
Days follow the container's local time (TZ).
"""
import datetime as dt
import json
import os
import threading
from pathlib import Path

KEEP_DAYS = 90
MAX_STEP = 5            # seconds credited at most for one sample (pause, slow loop)


def _day(now):
    return dt.datetime.fromtimestamp(now).strftime('%Y-%m-%d')


class TrackerStats:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.days = {}          # 'YYYY-MM-DD' -> domain -> {'bytes', 'seconds', 'torrents': set(hash prefix)}
        self.last = None
        self.dirty = False
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            for day, domains in data.get('days', {}).items():
                self.days[day] = {domain: {'bytes': int(v.get('bytes', 0)), 'seconds': int(v.get('seconds', 0)),
                                           'torrents': set(v.get('torrents', []))}
                                  for domain, v in domains.items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def add(self, now, rates, domains):
        """rates: infohash -> bytes/s (None while the measure is missing);
        domains: infohash -> tracker domain. Unknown domains are counted as « autre »."""
        with self.lock:
            step = 1 if self.last is None else max(0, min(MAX_STEP, now - self.last))
            self.last = now
            if not step:
                return
            day = self.days.setdefault(_day(now), {})
            for infohash, rate in rates.items():
                if rate is None:
                    continue
                entry = day.setdefault(domains.get(infohash) or 'autre', {'bytes': 0, 'seconds': 0, 'torrents': set()})
                entry['bytes'] += int(max(0, rate) * step)
                entry['seconds'] += int(step)
                entry['torrents'].add(infohash[:12])
                self.dirty = True

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
            self.days = {day: value for day, value in self.days.items() if day > cutoff}
            data = {'version': 1, 'days': {day: {domain: {'bytes': v['bytes'], 'seconds': v['seconds'],
                                                          'torrents': sorted(v['torrents'])}
                                                 for domain, v in domains.items()}
                                           for day, domains in self.days.items()}}
            self.dirty = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + '.tmp')
        temp.write_text(json.dumps(data, separators=(',', ':')), encoding='utf-8')
        os.replace(temp, self.path)

    def summary(self, days=30, today=None):
        """Per-tracker totals for today / 7 / 30 days and a daily series, plus overall totals."""
        today = today or dt.date.today()
        span = [(today - dt.timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
        week = set(span[-7:])
        with self.lock:
            snapshot = {day: {d: dict(v, torrents=set(v['torrents'])) for d, v in self.days.get(day, {}).items()}
                        for day in span}
        trackers = {}
        for day in span:
            for domain, value in snapshot[day].items():
                row = trackers.setdefault(domain, {'domain': domain, 'series': [0] * days, 'torrents': set(),
                                                   'today': {'bytes': 0, 'seconds': 0},
                                                   'week': {'bytes': 0, 'seconds': 0},
                                                   'month': {'bytes': 0, 'seconds': 0}})
                row['series'][span.index(day)] = value['bytes']
                row['torrents'] |= value['torrents']
                periods = ['month'] + (['week'] if day in week else []) + (['today'] if day == span[-1] else [])
                for period in periods:
                    row[period]['bytes'] += value['bytes']
                    row[period]['seconds'] += value['seconds']
        rows = sorted(trackers.values(), key=lambda r: (-r['month']['bytes'], r['domain']))
        for row in rows:
            row['torrents'] = len(row['torrents'])
        daily = [sum(snapshot[day][d]['bytes'] for d in snapshot[day]) for day in span]
        return {'days': span, 'daily': daily, 'trackers': rows,
                'totals': {period: sum(r[period]['bytes'] for r in rows) for period in ('today', 'week', 'month')}}
