"""
Utilities for parsing and interpreting crontab schedules

:author: Doug Skrypa
"""

import logging
from datetime import datetime
from functools import cached_property
from typing import Any, Union, Iterator, Iterable, Mapping

try:
    from bitarray import bitarray
except ImportError:
    bitarray = None

__all__ = ['CronSchedule', 'NestCronSchedule']
log = logging.getLogger(__name__)
CronDict = dict[Union[int, str], bool]
Bool = Union[bool, Any]


class TimePart:
    def __init__(self, cron: 'CronSchedule', name: str, intervals: int, min: int = 0, special: str = None):
        self.cron = cron
        self.name = name
        try:
            self.arr = bitarray(intervals)
        except TypeError as e:
            raise RuntimeError('Missing required dependency: bitarray (pip install nest-client[schedule])') from e
        self.min = min
        if special:
            self.special_keys = special
            self.special_vals = bitarray(len(special))
        else:
            self.special_keys = None
            self.special_vals = None
        self.reset()

    def reset(self, default: bool = True):
        self.arr.setall(default)
        if self.special_keys:
            self.special_vals.setall(False)

    def _offset(self, key: int) -> int:
        offset_key = key - self.min
        if offset_key < 0:
            raise IndexError(f'Invalid time={key} for part={self.name!r}')
        return offset_key

    def __getitem__(self, key: Union[str, int]) -> Bool:
        if isinstance(key, str):
            if keys := self.special_keys:
                try:
                    index = keys.index(key)
                except ValueError:
                    return False
                else:
                    return self.special_vals[index]
            return False
        elif isinstance(key, int):
            return self.arr[self._offset(key)]
        raise TypeError(f'Unexpected type={key.__class__.__name__} for {key=}')

    def __setitem__(self, key: Union[str, int], value: bool):
        if isinstance(key, str):
            if keys := self.special_keys:
                try:
                    index = keys.index(key)
                except ValueError as e:
                    raise KeyError(f'Invalid cron schedule {key=!r} in part={self.name!r}') from e
                else:
                    self.special_vals[index] = value
            else:
                raise KeyError(f'Invalid cron schedule {key=!r} in part={self.name!r}')
        elif isinstance(key, int):
            self.arr[self._offset(key)] = value
        else:
            raise TypeError(f'Unexpected type={key.__class__.__name__} for {key=}')

    def __iter__(self) -> Iterator[int]:
        for i, val in enumerate(self.arr, self.min):
            if val:
                yield i

    def __str__(self) -> str:
        arr = self.arr
        if arr.all():
            return '*'

        last = self['L']
        if not arr.any():
            if last:
                return 'L'
            # raise ValueError('Unexpected state')
            return 'X'

        if self.name == 'dow' and not self.cron.week.arr.all():
            week = self.cron.week
            weeks = list(week)
            if week['L']:
                weeks.append('L')
            return ','.join(f'{v}#{w}' for v in self for w in weeks)
        elif not last:
            for divisor in range(2, len(arr) // 2 + 1):
                divisible = bitarray(len(arr))
                divisible.setall(False)
                divisible[::divisor] = True
                if divisible == arr:
                    return f'*/{divisor}'

        collapsed = self._collapse_ranges()
        return f'{collapsed},L' if last else collapsed

    def __repr__(self):
        return f'<{self.__class__.__name__}[{self.name}: {self}]>'

    def all(self) -> bool:
        return all(val for i, val in enumerate(self.arr, self.min))

    def _collapse_ranges(self) -> str:
        ranges = []
        last = None
        for value in self:
            if last is None:
                ranges.append((value, value))
            elif value - last == 1:
                ranges[-1] = (ranges[-1][0], value)
            else:
                ranges.append((value, value))

            last = value

        return ','.join(str(a) if a == b else f'{a}-{b}' for a, b in ranges)

    def set(self, value: str):
        if value == '*':
            self.arr.setall(True)
        elif '/' in value:
            a, divisor = value.split('/', 1)
            if a != '*' or not divisor.isnumeric():
                raise ValueError(f'Invalid cron schedule {value=!r} in part={self.name!r}')
            self.arr.setall(False)
            self.arr[::int(divisor)] = True
        else:
            parts = set(value.split(','))
            if 'L' in parts:
                self['L'] = True
                parts.remove('L')
            if self.name == 'dow':
                _parts = set()
                weeks = set()
                for p in parts:
                    if '#' in p:
                        val, week = p.split('#')
                        _parts.add(val)
                        if week == 'L':
                            self.cron.week['L'] = True
                        else:
                            try:
                                week = int(week)
                            except (TypeError, ValueError):
                                raise ValueError(f'Invalid cron schedule {value=!r} in {self.name=}')
                            else:
                                if 1 <= week <= 4:
                                    weeks.add(week)
                                else:
                                    raise ValueError(f'Invalid cron schedule {value=!r} in {self.name=}')
                    else:
                        _parts.add(p)

                if weeks:
                    self.cron.week.set_intervals(weeks)
                parts = _parts

            vals = set()
            for p in parts:
                if '-' in p:
                    try:
                        a, b = map(int, p.split('-'))
                    except (TypeError, ValueError):
                        raise ValueError(f'Invalid cron schedule {value=!r} in {self.name=}')
                    if a >= b:
                        raise ValueError(f'Invalid cron schedule {value=!r} in {self.name=}')
                    vals.update(range(a, b + 1))
                else:
                    try:
                        vals.add(int(p))
                    except (TypeError, ValueError):
                        raise ValueError(f'Invalid cron schedule {value=!r} in {self.name=}')

            self.set_intervals(vals)

    def set_intervals(self, intervals: Union[Mapping[int, bool], Iterable[int]]):
        # log.debug(f'{self!r}: Setting {intervals=}')
        arr = self.arr
        arr.setall(False)
        if isinstance(intervals, Mapping):
            if _min := self.min:
                intervals = {k - _min: v for k, v in intervals.items()}

            # log.debug(f'{self!r}: Setting offset {intervals=}')
            for key, val in intervals.items():
                arr[key] = val
        else:
            if _min := self.min:
                intervals = [v - _min for v in intervals]
            # log.debug(f'{self!r}: Setting offset {intervals=}')
            for key in intervals:
                arr[key] = True

    def replace(self, key: Union[str, int], value: bool):
        self.reset(not value)
        self[key] = value


class CronPart:
    def __init__(self, intervals: int, min: int = 0, special: str = None):
        self.intervals = intervals
        self.min = min
        self.special = special

    def __set_name__(self, owner, name):
        self.name = name

    def _get(self, instance) -> TimePart:
        try:
            return instance.__dict__[self.name]
        except KeyError:
            instance.__dict__[self.name] = tp = TimePart(instance, self.name, self.intervals, self.min, self.special)
            return tp

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return self._get(instance)

    def __set__(self, instance, value):
        raise TypeError(f'{self.__class__.__name__} objects do not allow assignment')


class CronSchedule:
    second = CronPart(60)                   # Second
    minute = CronPart(60)                   # Minute
    hour = CronPart(24)                     # Hour
    day = CronPart(31, min=1, special='L')  # Day of month
    month = CronPart(12, min=1)             # Month
    dow = CronPart(7)                       # Day of week: 0 = Sunday, 1 = Monday, ... 6 = Saturday, 7 = Sunday
    week = CronPart(6, min=1, special='L')  # Week of month

    def __init__(self, start: datetime = None):
        self._start = start

    def __str__(self):
        return ' '.join(map(str, (self.second, self.minute, self.hour, self.day, self.month, self.dow)))

    def __repr__(self):
        return f'<{self.__class__.__name__}[{self}]>'

    def _set_time(self, dt_obj: datetime):
        if dt_obj is not None:
            self.second.replace(dt_obj.second, True)
            self.minute.replace(dt_obj.minute, True)
            self.hour.replace(dt_obj.hour, True)

    @classmethod
    def from_cron(cls, cron_str: str) -> 'CronSchedule':
        self = cls()
        attrs = (self.second, self.minute, self.hour, self.day, self.month, self.dow)
        for attr, value in zip(attrs, cron_str.split()):
            attr.set(value)
        return self

    @cached_property
    def start(self) -> datetime:
        if self._start:
            return self._start
        dt = datetime.now().replace(
            second=min(self.second), minute=min(self.minute), hour=min(self.hour), microsecond=0
        )
        return dt

    def reset(self):
        for attr in (self.second, self.minute, self.hour, self.day, self.month, self.dow):
            attr.reset()


class NestCronSchedule(CronSchedule):
    @classmethod
    def from_cron(cls, cron_str: str) -> 'NestCronSchedule':
        cron = super().from_cron(cron_str)
        if attr := next((attr for attr in ('day', 'month', 'week') if not getattr(cron, attr).all()), None):
            bad = getattr(cron, attr)
            raise ValueError(f'Nest schedules only support minutes, hours, and days of the week - {bad=!r}')
        return cron  # noqa

    def __iter__(self) -> Iterator[tuple[int, int]]:
        """
        :return iterator: Iterator that yields tuples of (day of week, time of day [seconds])
        """
        for dow in self.dow:
            for hour in self.hour:
                for minute in self.minute:
                    yield dow, (hour * 60 + minute) * 60
