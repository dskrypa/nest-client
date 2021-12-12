"""
Classes that represent Nest Structures, Users, Devices/Thermostats, etc.

:author: Doug Skrypa
"""

import calendar
import json
import logging
import time
from bisect import bisect_left
from dataclasses import dataclass, field, fields, asdict, InitVar
from datetime import datetime
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

    # @cached_property
    # def days(self) -> dict[int, 'DaySchedule']:
    #     return {int(day): DaySchedule(day, schedule.values()) for day, schedule in sorted(self._days.items())}

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

    # region Output / Formatting Methods

    def format(self, output_format: str = 'table', mode: str = 'pretty'):
        if output_format == 'table':
            if mode != 'pretty':
                raise ValueError(f'Invalid format {mode=} with {output_format=} for {self}')

            schedule = self.weekly_schedule.as_day_time_temp_map()
            rows = [{'Day': day, **time_temp_map} for day, time_temp_map in schedule.items() if time_temp_map]
            times = {t for time_temp_map in schedule.values() for t in time_temp_map if time_temp_map}
            columns = [SimpleColumn('Day'), *(SimpleColumn(_time, ftype='.1f') for _time in sorted(times))]
            table = Table(*columns, update_width=True)
            return table.format_rows(rows, True)
        else:
            schedule = self.weekly_schedule.as_day_time_temp_map() if mode == 'pretty' else self.to_dict()
            return Printer(output_format).pformat(schedule, sort_keys=False)

    def print(self, output_format: str = 'table', mode: str = 'pretty'):
        if output_format == 'table':
            print(f'Schedule name={self.name!r} mode={self.mode!r} ver={self.version!r}\n')
        print(self.format(output_format, mode))

    # endregion


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

    def as_day_time_temp_map(self) -> dict[str, dict[str, float] | None]:
        """Mapping of {day name: {'HH:MM': temperature}}"""
        convert = self.meta.unit == 'f'
        return {
            calendar.day_name[day_num]: sched.as_time_temp_map(convert) if (sched := self.days.get(day_num)) else None
            for day_num in (6, 0, 1, 2, 3, 4, 5)  # Su M Tu W Th F Sa
        }

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {'meta': self.meta.as_dict(), 'schedule': self.as_day_time_temp_map()}

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


class DaySchedule:
    def __init__(self, day: Day | int, schedule: Iterable[SchedEntry], parent: WeeklySchedule):
        if not 0 <= (day := _normalize_day(day)) <= 6:
            raise ValueError(f'Invalid {day=} - must be between 0=Monday and 6=Sunday, inclusive')
        self.num = day
        self.day = calendar.day_name[self.num]
        self.schedule = sorted(ScheduleEntry.from_dict(e) if isinstance(e, dict) else e for e in schedule)  # noqa
        self.parent = parent
        if parent.unit == 'f':
            for entry in self.schedule:
                entry.temp = round(f2c(entry.temp), 2)

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
            yield entry.time, entry.temp, entry.type

    def __bool__(self) -> bool:
        return bool(self.schedule)

    # endregion

    @classmethod
    def from_time_temp_map(cls, day: Day, time_temp_map: dict[TOD, float], parent: WeeklySchedule) -> 'DaySchedule':
        meta = parent.meta
        schedule = [
            ScheduleEntry(tod_secs(tod_str), temp, meta.mode, meta.user_id, meta.user_num)
            for tod_str, temp in time_temp_map.items()
        ]
        return cls(day, schedule, parent)

    def as_time_temp_map(self) -> dict[str, float]:
        if self.parent.unit == 'f':
            return {secs_to_wall(d_time): round(c2f(temp), 2) for d_time, temp, mode in self}
        else:
            return {secs_to_wall(d_time): temp for d_time, temp, mode in self}

    def as_dict(self) -> dict[str, 'ScheduleEntry']:
        return {str(i): entry for i, entry in enumerate(self.schedule)}

    def _time_pos(self, time_of_day: int) -> int:
        return bisect_left(self.schedule, time_of_day, key=lambda e: e.time)

    def insert(self, time_of_day: TOD, temp: float, user_id: str, user_num: int = 1, unit: str = 'c', mode: str = None):
        time_of_day = tod_secs(time_of_day)
        if unit[0].lower() == 'f':
            temp = round(f2c(temp), 2)
        entry = ScheduleEntry(time_of_day, temp, mode or self.parent.meta.unit, user_id, user_num)
        pos = self._time_pos(time_of_day)
        if pos and self.schedule[pos].time == time_of_day:
            self.schedule[pos] = entry
        else:
            self.schedule.insert(pos + 1, entry)

    def remove(self, time_of_day: TOD):
        time_of_day = tod_secs(time_of_day)
        pos = self._time_pos(time_of_day)
        if pos and self.schedule[pos].time == time_of_day:
            self.schedule.pop(pos)
        else:
            raise TimeNotFound(f'Invalid {time_of_day=} ({secs_to_wall(time_of_day)}) - not found in {self}')


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
        return cls(**{k: v for k in _fields(cls) if (v := entry.get(k)) is not None})

    def as_dict(self) -> dict[str, str | int | float]:
        return asdict(self)

    @property
    def is_continuation(self) -> bool:
        return self.entry_type == 'continuation'

    def make_continuation(self) -> 'ScheduleEntry':
        return ScheduleEntry(0, self.temp, self.type, None, 1, entry_type='continuation')


def _fields(obj):
    for field_obj in fields(obj):
        yield field_obj.name


def secs_to_wall(seconds: int) -> str:
    hour, minute = divmod(seconds // 60, 60)
    return f'{hour:02d}:{minute:02d}'


def wall_to_secs(wall: str) -> int:
    hour, minute = map(int, wall.split(':'))
    return (hour * 60 + minute) * 60


def tod_secs(time_of_day: TOD) -> int:
    if isinstance(time_of_day, str):
        hour, minute = map(int, time_of_day.split(':'))
        time_of_day = (hour * 60 + minute) * 60
    if not 0 <= time_of_day < 86400:
        tod_str = secs_to_wall(time_of_day)
        raise ValueError(f'Invalid {time_of_day=} ({tod_str}) - must be between 0 (0:00) and 86400 (24:00)')
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


def _next_day(day: int) -> int:
    return 0 if day == 6 else day + 1


def _continuation_day(day: int) -> int:
    days = list(range(7))
    candidates = days[day+1:] + days[:day]
    return candidates[0]
