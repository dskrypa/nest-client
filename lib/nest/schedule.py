"""
Library for interacting with the Nest thermostat via the cloud API

:author: Doug Skrypa
"""

import json
import logging
import time
from typing import TYPE_CHECKING, Union

from .cron import NestCronSchedule
from .exceptions import TimeNotFound
from .utils import fahrenheit_to_celsius as f2c

if TYPE_CHECKING:
    from .entities import Schedule

__all__ = ['NestSchedule']
log = logging.getLogger(__name__)


class NestSchedule:
    def __init__(self, raw_schedule: 'Schedule'):
        """
        .. important::
            Nest represents days as 0=Monday ~ 6=Sunday.  This class uses the same values as cron, i.e., 0=Sunday ~
            6=Saturday, and automatically converts between them where necessary.

        Old:
            def get_schedule(self: NestWebClient) -> NestSchedule:
                raw = self.app_launch(['schedule'], raw=True)['updated_buckets']
                return NestSchedule(self, raw)
        """
        self.raw = raw_schedule
        self.config = raw_schedule.config
        self.object_key = raw_schedule.key
        self.user_id = f'user.{raw_schedule.client.user_id}'
        self._schedule = {
            int(day): [entry for i, entry in sorted(sched.items())] for day, sched in sorted(raw_schedule.days.items())
        }

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
            'touched_by': self.raw.user_id_num_map[self.user_id],
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
        shared = self.raw.parent.shared
        active_mode = shared.mode.lower()
        schedule_mode = self.raw.mode.lower()
        if active_mode != schedule_mode:
            prefix = '[DRY RUN] Would update' if dry_run else 'Updating'
            log.info(f'{prefix} mode from {active_mode} to {schedule_mode}')
            if not dry_run:
                shared.set_mode(schedule_mode)

    def push(self, dry_run: bool = False):
        self._update_mode(dry_run)
        days = {
            str(day): {str(i): entry for i, entry in enumerate(entries)}
            for day, entries in sorted(self._schedule.items())
        }
        log.info(f'New schedule to be pushed:\n{self.format()}')
        log.debug('Full schedule to be pushed: {}'.format(json.dumps(days, indent=4, sort_keys=True)))
        prefix = '[DRY RUN] Would push' if dry_run else 'Pushing'
        schedule_mode = self.raw.mode.lower()
        log.info(f'{prefix} changes to {schedule_mode} schedule with name={self.raw.name!r}')
        if not dry_run:

            value = {'ver': self._ver, 'schedule_mode': self._schedule_mode, 'name': self._name, 'days': days}
            resp = self._nest._post_put(value, self.object_key, 'OVERWRITE')
            log.debug('Push response: {}'.format(json.dumps(resp.json(), indent=4, sort_keys=True)))

    # endregion

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
