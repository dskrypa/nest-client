"""
Library for interacting with the Nest thermostat via the cloud API

:author: Doug Skrypa
"""

import calendar
import json
import logging
import time
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union

from .cron import NestCronSchedule
from .exceptions import TimeNotFound
from .output import SimpleColumn, Table, Printer
from .utils import celsius_to_fahrenheit as c2f, fahrenheit_to_celsius as f2c, secs_to_wall, wall_to_secs

if TYPE_CHECKING:
    from .client import NestWebClient

__all__ = ['NestSchedule']
log = logging.getLogger(__name__)


class NestSchedule:
    def __init__(self, nest: 'NestWebClient', raw_schedules: list[dict[str, Any]]):
        """
        .. important::
            Nest represents days as 0=Monday ~ 6=Sunday.  This class uses the same values as cron, i.e., 0=Sunday ~
            6=Saturday, and automatically converts between them where necessary.

        Old:
            def get_schedule(self: NestWebClient) -> NestSchedule:
                raw = self.app_launch(['schedule'], raw=True)['updated_buckets']
                return NestSchedule(self, raw)

        :param nest: The :class:`NestWebClient` from which this schedule originated
        :param raw_schedules: The result of ``NestWebClient.app_launch(['schedule'], raw=True)['updated_buckets']``
        """
        # TODO: Accept new Schedule object instead
        self._nest = nest
        self.config = nest.config
        self.object_key = f'schedule.{self._nest.serial}'
        self.user_id = f'user.{self._nest.user_id}'
        for entry in raw_schedules:
            if entry.get('object_key') == self.object_key:
                self._raw = entry
                break
        else:
            raise ValueError(f'Unable to find an entry for {self.object_key=!r} in the provided raw_schedule')
        info = self._raw['value']
        self._ver = info['ver']
        self._schedule_mode = info['schedule_mode']
        self._name = info['name']
        self._schedule = {
            int(day): [entry for i, entry in sorted(sched.items())] for day, sched in sorted(info['days'].items())
        }

    # region Serialization / De-serialization

    @classmethod
    def from_file(cls, nest: 'NestWebClient', path: Union[str, Path]) -> 'NestSchedule':
        path = Path(path)
        if not path.is_file():
            raise ValueError(f'Invalid schedule path: {path}')

        with path.open('r', encoding='utf-8') as f:
            schedule = json.load(f)

        return cls.from_dict(nest, schedule)

    @classmethod
    def from_dict(cls, nest: 'NestWebClient', schedule: dict[str, Any]) -> 'NestSchedule':
        user_id = f'user.{nest.user_id}'
        meta = schedule['meta']
        user_num = meta['user_nums'][user_id]
        _days = schedule['days']
        convert = meta['unit'] == 'f'
        days = {}
        for day_num, day in enumerate(calendar.day_name):
            if day_schedule := _days.get(day):
                days[day_num] = {
                    i: {
                        'temp': f2c(temp) if convert else temp,
                        'touched_by': user_num,
                        'time': wall_to_secs(tod_str),
                        'touched_tzo': -14400,
                        'type': meta['mode'],
                        'entry_type': 'setpoint',
                        'touched_user_id': user_id,
                        'touched_at': int(time.time()),
                    }
                    for i, (tod_str, temp) in enumerate(day_schedule.items())
                }
            else:
                days[day_num] = {}

        raw_schedule = {
            'object_key': f'schedule.{nest.serial}',
            'value': {'ver': meta['ver'], 'schedule_mode': meta['mode'], 'name': meta['name'], 'days': days},
        }
        return cls(nest, [raw_schedule])

    def to_dict(self):
        schedule = {
            'meta': {
                'ver': self._ver,
                'mode': self._schedule_mode,
                'name': self._name,
                'unit': self.config.temp_unit,
                'user_nums': self.user_nums,
            },
            'days': self.as_day_time_temp_map(),
        }
        return schedule

    def save(self, path: Union[str, Path], overwrite: bool = False, dry_run: bool = False):
        path = Path(path)
        if path.is_file() and not overwrite:
            raise ValueError(f'Path already exists: {path}')
        elif not path.parent.exists() and not dry_run:
            path.parent.mkdir(parents=True)

        prefix = '[DRY RUN] Would save' if dry_run else 'Saving'
        log.info(f'{prefix} schedule to {path}')
        with path.open('w', encoding='utf-8', newline='\n') as f:
            json.dump(self.to_dict(), f, indent=4, sort_keys=False)

    # endregion

    # region Schedule Modifiers

    def update(self, cron_str: str, action: str, temp: float, dry_run: bool = False):
        cron = NestCronSchedule.from_cron(cron_str)
        changes_made = 0
        if action == 'remove':
            for dow, tod_seconds in cron:
                try:
                    self.remove(dow, tod_seconds)
                except TimeNotFound as e:
                    log.debug(e)
                    pass
                else:
                    log.debug(f'Removed time={secs_to_wall(tod_seconds)} from {dow=}')
                    changes_made += 1
        elif action == 'add':
            for dow, tod_seconds in cron:
                self.insert(dow, tod_seconds, temp)
                changes_made += 1
        else:
            raise ValueError(f'Unexpected {action=!r}')

        if changes_made:
            past, tf = ('Added', 'to') if action == 'add' else ('Removed', 'from')
            log.info(f'{past} {changes_made} entries {tf} {self._schedule_mode} schedule with name={self._name!r}')
            self.push(dry_run)
        else:
            log.info(f'No changes made')

    def insert(self, day: int, time_of_day: Union[str, int], temp: float):
        if not 0 <= day < 7:
            raise ValueError(f'Invalid {day=!r} - Expected 0=Sunday ~ 6=Saturday')
        temp = f2c(temp) if self.config.temp_unit == 'f' else temp
        time_of_day = wall_to_secs(time_of_day) if isinstance(time_of_day, str) else time_of_day
        if not 0 <= time_of_day < 86400:
            raise ValueError(f'Invalid {time_of_day=!r} ({secs_to_wall(time_of_day)}) - must be > 0 and < 86400')

        entry = {
            'temp': temp,
            'touched_by': self._user_num,
            'time': time_of_day,
            'touched_tzo': -14400,
            'type': self._schedule_mode,
            'entry_type': 'setpoint',
            'touched_user_id': self.user_id,
            'touched_at': int(time.time()),
        }
        day_schedule = self._schedule.setdefault(_previous_day(day), [])
        for i, existing in enumerate(day_schedule):
            if existing['time'] == time_of_day:
                day_schedule[i] = entry
                break
        else:
            day_schedule.append(entry)
        self._update_continuations()

    def remove(self, day: int, time_of_day: Union[str, int]):
        if not 0 <= day < 7:
            raise ValueError(f'Invalid {day=!r} - Expected 0=Sunday ~ 6=Saturday')
        time_of_day = wall_to_secs(time_of_day) if isinstance(time_of_day, str) else time_of_day
        if not 0 < time_of_day < 86400:
            raise ValueError(f'Invalid {time_of_day=!r} ({secs_to_wall(time_of_day)}) - must be > 0 and < 86400')

        day_entries = self._schedule.setdefault(_previous_day(day), [])
        index = next((i for i, entry in enumerate(day_entries) if entry['time'] == time_of_day), None)
        if index is None:
            times = ', '.join(sorted(secs_to_wall(e['time']) for e in day_entries))
            raise TimeNotFound(
                f'Invalid {time_of_day=!r} ({secs_to_wall(time_of_day)}) - not found in {day=} with times: {times}'
            )
        day_entries.pop(index)
        self._update_continuations()

    def _update_mode(self, dry_run: bool = False):
        mode = self._nest.get_mode().lower()
        sched_mode = self._schedule_mode.lower()
        if sched_mode != mode:
            prefix = '[DRY RUN] Would update' if dry_run else 'Updating'
            log.info(f'{prefix} mode from {mode} to {sched_mode}')
            if not dry_run:
                self._nest.set_mode(sched_mode)

    def push(self, dry_run: bool = False):
        self._update_mode(dry_run)
        days = {
            str(day): {str(i): entry for i, entry in enumerate(entries)}
            for day, entries in sorted(self._schedule.items())
        }
        log.info(f'New schedule to be pushed:\n{self.format()}')
        log.debug('Full schedule to be pushed: {}'.format(json.dumps(days, indent=4, sort_keys=True)))
        prefix = '[DRY RUN] Would push' if dry_run else 'Pushing'
        log.info(f'{prefix} changes to {self._schedule_mode} schedule with name={self._name!r}')
        if not dry_run:
            value = {'ver': self._ver, 'schedule_mode': self._schedule_mode, 'name': self._name, 'days': days}
            resp = self._nest._post_put(value, self.object_key, 'OVERWRITE')
            log.debug('Push response: {}'.format(json.dumps(resp.json(), indent=4, sort_keys=True)))

    # endregion

    def as_day_time_temp_map(self):
        day_names = calendar.day_name[-1:] + calendar.day_name[:-1]
        day_time_temp_map = {day: None for day in day_names}
        convert = self.config.temp_unit == 'f'
        for day, (day_num, day_schedule) in zip(calendar.day_name, sorted(self._schedule.items())):
            day_time_temp_map[day] = {
                secs_to_wall(entry['time']): round(c2f(entry['temp']), 2) if convert else entry['temp']
                for i, entry in enumerate(day_schedule)
            }
        return day_time_temp_map

    def format(self, output_format: str = 'table'):
        schedule = self.as_day_time_temp_map()
        if output_format == 'table':
            times = set()
            rows = []
            for day, time_temp_map in schedule.items():
                times.update(time_temp_map)
                row = time_temp_map.copy()
                row['Day'] = day
                rows.append(row)

            columns = [SimpleColumn('Day')]
            columns.extend(SimpleColumn(_time, ftype='.1f') for _time in sorted(times))
            table = Table(*columns, update_width=True)
            return table.format_rows(rows, True)
        else:
            return Printer(output_format).pformat(schedule, sort_keys=False)

    def print(self, output_format: str = 'table'):
        if output_format == 'table':
            print(f'Schedule name={self._name!r} mode={self._schedule_mode!r} ver={self._ver!r}\n')
        print(self.format(output_format))

    @cached_property
    def user_nums(self):
        return {
            e['touched_user_id']: e['touched_by']
            for d, entries in self._schedule.items()
            for e in entries if 'touched_user_id' in e
        }

    @cached_property
    def _user_num(self):
        return self.user_nums[self.user_id]

    def _find_last(self, day: int):
        while (prev_day := _previous_day(day)) != day:
            if entries := self._schedule.setdefault(prev_day, []):
                entries.sort(key=lambda e: e['time'])
                return entries[-1].copy()
        return None

    def _update_continuations(self):
        for day in range(7):
            today = self._schedule.setdefault(day, [])
            today.sort(key=lambda e: e['time'])
            if continuation := self._find_last(day):
                if today[0]['entry_type'] == 'continuation' and today[0]['temp'] != continuation['temp']:
                    log.debug(f'Updating continuation entry for {day=}')
                    continuation.pop('touched_user_id', None)
                    continuation.update(touched_by=1, time=0, entry_type='continuation')
                    today[0] = continuation
                else:
                    log.debug(f'The continuation entry for {day=} is already correct')
            else:
                # this is a new schedule - update every day to continue the last entry from today & break
                continuation = today[-1].copy()
                continuation.pop('touched_user_id', None)
                continuation.update(touched_by=1, time=0, entry_type='continuation')
                for _day in range(7):
                    log.debug(f'Adding continuation entry for day={_day}')
                    day_sched = self._schedule.setdefault(_day, [])
                    if not any(e['time'] == 0 for e in day_sched):
                        day_sched.insert(0, continuation)
                break


def _previous_day(day: int) -> int:
    return 6 if day == 0 else day - 1


def _next_day(day: int) -> int:
    return 0 if day == 6 else day + 1


def _continuation_day(day: int) -> int:
    days = list(range(7))
    candidates = days[day+1:] + days[:day]
    return candidates[0]
