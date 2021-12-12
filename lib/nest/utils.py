"""
Utils for working with Nest thermostats
"""

import os
from abc import ABC
from functools import cached_property
from getpass import getuser
from pathlib import Path
from tempfile import gettempdir

from .__version__ import __title__ as pkg_name

__all__ = [
    'ClearableCachedPropertyMixin',
    'get_user_cache_dir',
    'get_user_temp_dir',
    'celsius_to_fahrenheit',
    'fahrenheit_to_celsius',
    'cached_classproperty',
    'format_duration',
]
ON_WINDOWS = os.name == 'nt'


# region Unit Conversion Functions

def celsius_to_fahrenheit(deg_c: float) -> float:
    return (deg_c * 9 / 5) + 32


def fahrenheit_to_celsius(deg_f: float) -> float:
    return (deg_f - 32) * 5 / 9

# endregion


# region Temp Dir Functions


def get_user_cache_dir(subdir: str = None, mode: int = 0o777) -> Path:
    cache_dir = get_user_temp_dir(*filter(None, (pkg_name, subdir)), mode=mode)
    if not cache_dir.is_dir():
        raise ValueError(f'Invalid path - not a directory: {cache_dir.as_posix()}')
    return cache_dir


def get_user_temp_dir(*sub_dirs, mode: int = 0o777) -> Path:
    """
    On Windows, returns `~/AppData/Local/Temp` or a sub-directory named after the current user of another temporary
    directory.  On Linux, returns a sub-directory named after the current user in `/tmp`, `/var/tmp`, or `/usr/tmp`.
    :param sub_dirs: Child directories of the chosen directory to include/create
    :param mode: Permissions to set if the directory needs to be created (0o777 by default, which matches the default
      for :meth:`pathlib.Path.mkdir`)
    """
    path = Path(gettempdir())
    if not ON_WINDOWS or not path.as_posix().endswith('AppData/Local/Temp'):
        path = path.joinpath(getuser())
    if sub_dirs:
        path = path.joinpath(*sub_dirs)
    if not path.exists():
        path.mkdir(mode=mode, parents=True)
    return path


# endregion


# region Clearable Cached Properties


class ClearableCachedProperty(ABC):
    pass


# noinspection PyUnresolvedReferences
ClearableCachedProperty.register(cached_property)


class ClearableCachedPropertyMixin:
    @classmethod
    def _cached_properties(cls):
        cached_properties = {}
        for clz in cls.mro():
            if clz == cls:
                for k, v in cls.__dict__.items():
                    if isinstance(v, ClearableCachedProperty):
                        cached_properties[k] = v
            else:
                try:
                    cached_properties.update(clz._cached_properties())  # noqa
                except AttributeError:
                    pass
        return cached_properties

    def clear_cached_properties(self):
        for prop in self._cached_properties():
            try:
                del self.__dict__[prop]
            except KeyError:
                pass


class cached_classproperty:
    def __init__(self, func):
        self.__doc__ = func.__doc__
        if not isinstance(func, (classmethod, staticmethod)):
            func = classmethod(func)
        self.func = func
        self.values = {}

    def __get__(self, obj, cls):
        try:
            return self.values[cls]
        except KeyError:
            self.values[cls] = value = self.func.__get__(obj, cls)()  # noqa
            return value

# endregion


def format_duration(seconds: float) -> str:
    """
    Formats time in seconds as (Dd)HH:MM:SS (time.stfrtime() is not useful for formatting durations).

    :param seconds: Number of seconds to format
    :return: Given number of seconds as (Dd)HH:MM:SS
    """
    x = '-' if seconds < 0 else ''
    m, s = divmod(abs(seconds), 60)
    h, m = divmod(int(m), 60)
    d, h = divmod(h, 24)
    x = f'{x}{d}d' if d > 0 else x
    return f'{x}{h:02d}:{m:02d}:{s:02d}' if isinstance(s, int) else f'{x}{h:02d}:{m:02d}:{s:05.2f}'
