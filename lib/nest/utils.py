"""
Utils for working with Nest thermostats
"""

import os
from abc import ABC
from functools import cached_property
from getpass import getuser
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING, Any, Callable

from .__version__ import __title__ as pkg_name
from .exceptions import DictAttrFieldNotFoundError

if TYPE_CHECKING:
    from .entities import NestObject

__all__ = [
    'ClearableCachedPropertyMixin',
    'NestProperty',
    'TemperatureProperty',
    'get_user_cache_dir',
    'get_user_temp_dir',
    'celsius_to_fahrenheit',
    'fahrenheit_to_celsius',
    'cached_classproperty',
]
ON_WINDOWS = os.name == 'nt'
_NotSet = object()


# region Unit Conversion Functions

def celsius_to_fahrenheit(deg_c: float) -> float:
    return (deg_c * 9 / 5) + 32


def fahrenheit_to_celsius(deg_f: float) -> float:
    return (deg_f - 32) * 5 / 9


def secs_to_wall(seconds: int) -> str:
    hour, minute = divmod(seconds // 60, 60)
    return f'{hour:02d}:{minute:02d}'


def wall_to_secs(wall: str) -> int:
    hour, minute = map(int, wall.split(':'))
    return (hour * 60 + minute) * 60

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
    _set_name = False

    def __set_name__(self, owner, name):
        if self._set_name:
            self.name = name


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


class NestProperty(ClearableCachedProperty):
    def __init__(
        self,
        path: str,
        type: Callable = _NotSet,  # noqa
        default: Any = _NotSet,
        default_factory: Callable = _NotSet,
        delim: str = '.',
        attr: str = 'value',
    ):
        # noinspection PyUnresolvedReferences
        """
        Descriptor that acts as a cached property for retrieving values nested in a dict stored in an attribute of the
        object that this :class:`NestProperty` is a member of.  The value is not accessed or stored until the first
        time that it is accessed.

        To un-cache a value (causes the descriptor to take over again)::\n
            >>> del instance.__dict__[attr_name]

        The :class:`ClearableCachedPropertyMixin` mixin class can be used to facilitate clearing all
        :class:`NestProperty` and any similar cached properties that exist in a given object.

        :param path: The nexted key location in the dict attribute of the value that this NestProperty
          represents; dict keys should be separated by ``.``, otherwise the delimiter should be provided via ``delim``
        :param type: Callable that accepts 1 argument; the value of this NestProperty will be passed to it,
          and the result will be returned as this NestProperty's value (default: no conversion)
        :param default: Default value to return if a KeyError is encountered while accessing the given path
        :param default_factory: Callable that accepts no arguments to be used to generate default values
          instead of an explicit default value
        :param delim: Separator that was used between keys in the provided path (default: ``.``)
        :param attr: Name of the attribute in the class that this NestProperty is in that contains the dict that this
          NestProperty should reference
        """
        self.path = [p for p in path.split(delim) if p]
        self.path_repr = delim.join(self.path)
        self.attr = attr
        self.type = type
        self.name = f'_{self.__class__.__name__}#{self.path_repr}'
        self.default = default
        self.default_factory = default_factory

    def __set_name__(self, owner, name):
        self.name = name
        attr_path = ''.join('[{!r}]'.format(p) for p in self.path)
        self.__doc__ = (
            f'A :class:`NestProperty<nest.utils.NestProperty>` that references this {owner.__name__} instance\'s'
            f' {self.attr}{attr_path}'
        )

    def __get__(self, obj: 'NestObject', cls):
        if obj is None:
            return self

        if obj._needs_update:
            obj.refresh()

        value = getattr(obj, self.attr)
        for key in self.path:
            try:
                value = value[key]
            except KeyError:
                if self.default is not _NotSet:
                    value = self.default
                    break
                elif self.default_factory is not _NotSet:
                    value = self.default_factory()
                    break
                raise DictAttrFieldNotFoundError(obj, self.name, self.attr, self.path_repr)

        if self.type is not _NotSet:
            # noinspection PyArgumentList
            value = self.type(value)
        if '#' not in self.name:
            obj.__dict__[self.name] = value
        return value


class TemperatureProperty(NestProperty):
    def __get__(self, obj: 'NestObject', cls):
        if obj is None:
            return self
        value_c = super().__get__(obj, cls)
        if obj.client.config.temp_unit == 'f':
            return celsius_to_fahrenheit(value_c)
        return value_c


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


class replacement_itemgetter:
    """
    Return a callable object that fetches the given item(s) from its operand.
    After f = itemgetter(2), the call f(r) returns r[2].
    After g = itemgetter(2, 5, 3), the call g(r) returns (r[2], r[5], r[3])
    """
    __slots__ = ('_items', '_call', '_repl')

    def __init__(self, item, *items, replacements=None):
        self._repl = replacements or {}
        if not items:
            self._items = (item,)

            def func(obj):
                val = obj[item]
                try:
                    return self._repl[val]
                except KeyError:
                    return val

            self._call = func
        else:
            self._items = items = (item,) + items

            def func(obj):
                vals = []
                for val in (obj[i] for i in items):
                    try:
                        vals.append(self._repl[val])
                    except KeyError:
                        vals.append(val)
                return tuple(vals)

            self._call = func

    def __call__(self, obj):
        return self._call(obj)
