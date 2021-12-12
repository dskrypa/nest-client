"""
Classes that represent a Nest Schedule, and the week, days, and entries in it.

:author: Doug Skrypa
"""

import calendar
import json
import logging
import time
from bisect import bisect_left
from dataclasses import dataclass, field, fields, asdict, InitVar
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union, Iterator, Iterable

from ..constants import NEST_WHERE_MAP
from ..cron import NestCronSchedule
from ..exceptions import TimeNotFound
from ..output import SimpleColumn, Table, Printer
from ..utils import fahrenheit_to_celsius as f2c, celsius_to_fahrenheit as c2f
from .base import NestObject, NestProperty
from .device import NestDevice

if TYPE_CHECKING:
    from ..client import NestWebClient

__all__ = ['Schedule', 'DaySchedule', 'ScheduleEntry']
log = logging.getLogger(__name__)

ScheduleEntryDict = dict[str, str | int | float]
SchedEntry = Union['ScheduleEntry', ScheduleEntryDict]
Day = str | int  # Day name | 0 (Monday) - 6 (Sunday)
TOD = str | int  # HH:MM | 0 (midnight) - 86340 (23:59)


class Schedule(NestObject, type='schedule', parent_type='device'):
    parent: NestDevice
    name = NestProperty('name')
    version = NestProperty('ver')  # type: int
    mode = NestProperty('schedule_mode')  # type: str
    _days = NestProperty('days')  # type: dict[str, dict[str, ScheduleEntryDict]]
    where_id = NestProperty('where_id')

    # region Class Methods

    @classmethod
    def from_weekly(cls, client: 'NestWebClient', weekly_schedule: 'WeeklySchedule') -> 'Schedule':
        days_dict = {str(num): day_schedule.as_dict() for num, day_schedule in weekly_schedule.days.items()}
        meta = weekly_schedule.meta
        value = {'days': days_dict, 'name': meta.name, 'schedule_mode': meta.mode.upper(), 'ver': meta.ver}
        serial = meta.serial or client.config.serial or client.get_device().serial  # exc if many/no devices are found
        return cls(f'schedule.{serial}', None, None, value, client)

    @classmethod
    def from_file(cls, client: 'NestWebClient', path: Union[str, Path]) -> 'Schedule':
        return cls.from_weekly(client, WeeklySchedule.from_file(path))

    # endregion

    # region Properties

    @cached_property
    def where(self) -> str:
        return NEST_WHERE_MAP.get(self.where_id, self.where_id)

    @cached_property
    def user_id_num_map(self) -> dict[str, int]:
        return {entry['touched_user_id']: entry['touched_by'] for day in self._days.values() for entry in day.values()}

    @cached_property
    def meta(self) -> 'ScheduleMeta':
        user_id = f'user.{self.client.user_id}'
        user_num = self.user_id_num_map[user_id]
        return ScheduleMeta(self.serial, self.name, self.mode, user_id, self.config.temp_unit, user_num, self.version)

    @cached_property
    def weekly_schedule(self) -> 'WeeklySchedule':
        return WeeklySchedule(self.meta, self._days)

    # endregion

    def save(self, path: Union[str, Path], overwrite: bool = False, dry_run: bool = False):
        path = Path(path)
        if path.is_file() and not overwrite:
            raise ValueError(f'Path already exists: {path}')
        elif not path.parent.exists() and not dry_run:
            path.parent.mkdir(parents=True)

        log.info('{} schedule to {}'.format('[DRY RUN] Would save' if dry_run else 'Saving', path.as_posix()))
        if not dry_run:
            with path.open('w', encoding='utf-8', newline='\n') as f:
                json.dump(self.weekly_schedule.to_dict(), f, indent=4, sort_keys=False)

    def update(self, cron_str: str, action: str, temp: float, dry_run: bool = False):
        changes_made = self.weekly_schedule.update(cron_str, action, temp)
        if changes_made:
            self.push(dry_run)

    def push(self, dry_run: bool = False):
        self.parent.shared.maybe_update_mode(self.mode, dry_run)
        payload = self.weekly_schedule.to_update_dict()
        log.info(f'New schedule to be pushed:\n{self.weekly_schedule.format()}')
        log.debug('Full payload to be pushed: {}'.format(json.dumps(payload, indent=4, sort_keys=True)))
        prefix = '[DRY RUN] Would push' if dry_run else 'Pushing'
        schedule_mode = self.mode.lower()
        log.info(f'{prefix} changes to {schedule_mode} schedule with name={self.name!r}')
        if not dry_run:
            resp = self._set_full(payload, 'OVERWRITE')
            log.debug('Push response: {}'.format(json.dumps(resp.json(), indent=4, sort_keys=True)))

    def print(self, output_format: str = 'table', full: bool = False, unit: str = None, raw: bool = False):
        if raw:
            Printer(output_format).pprint(self.to_dict(), sort_keys=False, indent_nested_lists=True)
        else:
            self.weekly_schedule.print(output_format, full, unit)


class WeeklySchedule:
    days: dict[int, 'DaySchedule']

    def __init__(self, meta: 'ScheduleMeta', day_schedules: dict[str, dict[str, ScheduleEntryDict]]):
        self.meta = meta
        self.days = {int(n): DaySchedule(n, schedule.values(), self) for n, schedule in sorted(day_schedules.items())}

    @cached_property
    def unit(self) -> str:
        return self.meta.unit[0].lower()

    def __getitem__(self, day: Day) -> 'DaySchedule':
        day = _normalize_day(day)
        try:
            return self.days[day]
        except KeyError:
            if 0 <= day <= 6:
                self.days[day] = day_schedule = DaySchedule(day, (), self)
                return day_schedule
            raise

    def __iter__(self) -> Iterator['DaySchedule']:
        for day in range(7):
            yield self[day]

    def as_day_time_temp_map(self, convert: bool = None) -> dict[str, dict[str, float] | None]:
        """Mapping of {day name: {'HH:MM': temperature}}"""
        return {
            calendar.day_name[day_num]: ds.as_time_temp_map(convert) if (ds := self.days.get(day_num)) else None
            for day_num in (6, 0, 1, 2, 3, 4, 5)  # Su M Tu W Th F Sa
        }

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {'meta': self.meta.as_dict(), 'schedule': self.as_day_time_temp_map()}

    def to_update_dict(self) -> dict[str, str | int | dict[str, ScheduleEntryDict]]:
        days = {str(i): day_schedule.to_update_dict() for i, day_schedule in enumerate(self)}
        return {'ver': self.meta.ver, 'schedule_mode': self.meta.mode, 'name': self.meta.name, 'days': days}

    @classmethod
    def from_dict(cls, data: dict[str, dict[str, Any]]) -> 'WeeklySchedule':
        name2num = {name: num for num, name in enumerate(calendar.day_name)}
        self = cls(ScheduleMeta(**data['meta']), {})
        days = sorted(
            DaySchedule.from_time_temp_map(name2num[day_name], time_temp_map, self)
            for day_name, time_temp_map in data['schedule'].items()
        )
        self.days = {day.num: day for day in days}
        return self

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> 'WeeklySchedule':
        path = Path(path)
        if not path.is_file():
            raise ValueError(f'Invalid schedule path: {path}')
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)

    # region Update Methods

    def update(self, cron_str: str, action: str, temp: float) -> int:
        cron = NestCronSchedule.from_cron(cron_str)  # Note: cron DOW uses 0=Sunday
        changes_made = 0
        if action == 'remove':
            for dow, tod_seconds in cron:
                try:
                    self.remove(_previous_day(dow), tod_seconds)
                except TimeNotFound as e:
                    log.debug(e)
                else:
                    changes_made += 1
        elif action == 'add':
            for dow, tod_seconds in cron:
                self.insert(_previous_day(dow), tod_seconds, temp)
                changes_made += 1
        else:
            raise ValueError(f'Unexpected {action=!r}')

        if changes_made:
            past, tf = ('Added', 'to') if action == 'add' else ('Removed', 'from')
            log.info(f'{past} {changes_made} entries {tf} {self.meta.mode} schedule with name={self.meta.name!r}')
        else:
            log.info(f'No changes made')
        return changes_made

    def insert(self, day: Day, *args, **kwargs):
        self[day].insert(*args, **kwargs)
        self._update_continuations()

    def remove(self, day: Day, time_of_day: TOD):
        self[day].remove(time_of_day)
        self._update_continuations()

    def _update_continuations(self):
        yesterday = self[6]
        for today in self:
            if yesterday and (last_entry := yesterday[-1]):
                if not today:
                    log.debug(f'Adding continuation entry to {today}')
                    today.schedule.append(last_entry.make_continuation())
                else:
                    first = today[0]
                    if first.is_continuation:
                        if first.temp == last_entry.temp:
                            log.debug(f'The continuation entry for {today} is already correct')
                        else:
                            cont = last_entry.make_continuation()
                            log.debug(f'Updating continuation entry in {today} from {first} to {cont}')
                            today[0] = cont
                    elif first.time > 0:  # noqa
                        log.debug(f'Adding continuation entry to {today}')
                        today.schedule.insert(0, last_entry.make_continuation())
                    else:
                        log.debug(f'No continuation entry is needed for {today} due to explicit temp at time=0')
                yesterday = today

    # endregion

    # region Output / Formatting Methods

    def format(self, output_format: str = 'table', full: bool = False, unit: str = None):
        convert = None if unit is None else unit.lower()[0] == 'f'
        if output_format == 'table':
            schedule = self.as_day_time_temp_map(convert)
            rows = [{'Day': day, **time_temp_map} for day, time_temp_map in schedule.items() if time_temp_map]
            times = {t for time_temp_map in schedule.values() for t in time_temp_map if time_temp_map}
            columns = [SimpleColumn('Day'), *(SimpleColumn(_time, ftype='.1f') for _time in sorted(times))]
            table = Table(*columns, update_width=True)
            return table.format_rows(rows, True)
        else:
            schedule = self.to_update_dict() if full else self.as_day_time_temp_map(convert)
            return Printer(output_format).pformat(schedule, sort_keys=False, indent_nested_lists=True)

    def print(self, output_format: str = 'table', full: bool = False, unit: str = None):
        if output_format == 'table':
            meta = self.meta
            _unit = unit.lower()[0] if unit else self.unit
            print(f'Schedule name={meta.name!r} mode={meta.mode!r} ver={meta.ver!r} unit={_unit}\n')
        print(self.format(output_format, full, unit))

    # endregion


class DaySchedule:
    schedule: list['ScheduleEntry']

    def __init__(self, day: Day | int, schedule: Iterable[SchedEntry], parent: WeeklySchedule):
        if not 0 <= (day := _normalize_day(day)) <= 6:
            raise ValueError(f'Invalid {day=} - must be between 0=Monday and 6=Sunday, inclusive')
        self.num = day
        self.day = calendar.day_name[self.num]
        self.schedule = sorted(ScheduleEntry.from_dict(e) if isinstance(e, dict) else e for e in schedule)  # noqa
        self.parent = parent

    # region Dunder Methods

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__}[{self.day}: {self.as_time_temp_map()}]>'

    def __lt__(self, other: 'DaySchedule') -> bool:
        return self.num < other.num

    def __eq__(self, other: 'DaySchedule') -> bool:
        return self.num == other.num and self.schedule == other.schedule

    def __getitem__(self, index: int) -> 'ScheduleEntry':
        return self.schedule[index]

    def __setitem__(self, index: int, entry: SchedEntry):
        self.schedule[index] = ScheduleEntry.from_dict(entry) if isinstance(entry, dict) else entry

    def __iter__(self) -> Iterator[tuple[int, float, str]]:
        for entry in self.schedule:
            yield entry.time, entry.temp, entry.type  # noqa

    def __bool__(self) -> bool:
        return bool(self.schedule)

    # endregion

    @classmethod
    def from_time_temp_map(cls, day: Day, time_temp_map: dict[TOD, float], parent: WeeklySchedule) -> 'DaySchedule':
        meta = parent.meta
        norm_temp = (lambda t: round(f2c(t), 2)) if parent.unit == 'f' else (lambda t: t)
        schedule = [
            ScheduleEntry(tod_secs(tod_str), norm_temp(temp), meta.mode, meta.user_id, meta.user_num)
            for tod_str, temp in time_temp_map.items()
        ]
        return cls(day, schedule, parent)

    def as_time_temp_map(self, convert: bool = None) -> dict[str, float]:
        if convert or (convert is None and self.parent.unit == 'f'):
            return {secs_to_wall(d_time): round(c2f(temp), 2) for d_time, temp, mode in self}
        else:
            return {secs_to_wall(d_time): temp for d_time, temp, mode in self}

    def as_dict(self) -> dict[str, 'ScheduleEntry']:
        return {str(i): entry for i, entry in enumerate(self.schedule)}

    def to_update_dict(self) -> dict[str, ScheduleEntryDict]:
        return {str(i): entry.as_dict() for i, entry in enumerate(self.schedule)}

    def _time_pos(self, time_of_day: int) -> int:
        return bisect_left(self.schedule, time_of_day, key=lambda e: e.time)

    def insert(self, time_of_day: TOD, temp: float, user_id: str, user_num: int = 1, unit: str = 'c', mode: str = None):
        time_of_day = tod_secs(time_of_day)
        if unit[0].lower() == 'f':
            temp = round(f2c(temp), 2)
        entry = ScheduleEntry(time_of_day, temp, mode or self.parent.meta.unit, user_id, user_num)
        pos = self._time_pos(time_of_day)
        if pos and self.schedule[pos].time == time_of_day:  # noqa
            log.debug(f'Replacing entry for {tod_repr(time_of_day)} with {entry=} in {self}')
            self.schedule[pos] = entry
        else:
            log.debug(f'Inserting {entry=} for {tod_repr(time_of_day)} in {self}')
            self.schedule.insert(pos + 1, entry)

    def remove(self, time_of_day: TOD):
        time_of_day = tod_secs(time_of_day)
        pos = self._time_pos(time_of_day)
        if pos and self.schedule[pos].time == time_of_day:  # noqa
            log.debug(f'Removing {tod_repr(time_of_day)} from {self}')
            self.schedule.pop(pos)
        else:
            raise TimeNotFound(f'Invalid {tod_repr(time_of_day)} - not found in {self}')


@dataclass
class ScheduleMeta:
    serial: str
    name: str
    mode: str
    user_id: str
    unit: str = 'c'
    user_num: int = 1
    ver: int = 2

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(order=True)
class ScheduleEntry:
    time: InitVar[int] = field(compare=True)
    temp: float = field(compare=False)
    type: str = field(compare=False)
    touched_user_id: str | None = field(compare=False)
    touched_by: int = field(compare=False, default=1)
    touched_tzo: int = field(compare=False, default=-14400)
    entry_type: str = field(compare=False, default='setpoint')
    touched_at: int = field(compare=False, default_factory=lambda: int(time.time()))

    def __post_init__(self, time: TOD):  # noqa
        self.time = tod_secs(time)  # noqa

    @classmethod
    def from_dict(cls, entry: dict[str, str | int | float]) -> 'ScheduleEntry':
        entry.setdefault('type', entry.pop('mode', None))
        return cls(entry['time'], **{k: v for k in (f.name for f in fields(cls)) if (v := entry.get(k)) is not None})

    def as_dict(self) -> dict[str, str | int | float]:
        return asdict(self)

    @property
    def is_continuation(self) -> bool:
        return self.entry_type == 'continuation'

    def make_continuation(self) -> 'ScheduleEntry':
        return ScheduleEntry(0, self.temp, self.type, None, 1, entry_type='continuation')


def secs_to_wall(seconds: int) -> str:
    hour, minute = divmod(seconds // 60, 60)
    return f'{hour:02d}:{minute:02d}'


def tod_repr(time_of_day: int) -> str:
    return f'{time_of_day=} ({secs_to_wall(time_of_day)})'


def tod_secs(time_of_day: TOD) -> int:
    if isinstance(time_of_day, str):
        hour, minute = map(int, time_of_day.split(':'))
        time_of_day = (hour * 60 + minute) * 60
    if not 0 <= time_of_day < 86400:
        raise ValueError(f'Invalid {tod_repr(time_of_day)} - must be between 0 (0:00) and 86400 (24:00)')
    return time_of_day


def _normalize_day(day: str | int) -> int:
    if isinstance(day, str):
        try:
            names_index = _normalize_day._names_index
        except AttributeError:
            _normalize_day._names_index = names_index = list(map(str.lower, calendar.day_name)).index

        return int(day) if day.isnumeric() else names_index(day.lower())
    else:
        return day


def _previous_day(day: int) -> int:
    return 6 if day == 0 else day - 1
