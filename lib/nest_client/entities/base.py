"""
Classes that represent Nest Structures, Users, Devices/Thermostats, etc.

:author: Doug Skrypa
"""

from __future__ import annotations

import logging
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING, Any, TypeVar, Type, Callable

from ..constants import BUCKET_CHILD_TYPES
from ..exceptions import NestObjectNotFound, DictAttrFieldNotFoundError
from ..utils import ClearableCachedProperty, ClearableCachedPropertyMixin, cached_classproperty, celsius_to_fahrenheit

if TYPE_CHECKING:
    from httpx import Response
    from ..client import NestWebClient

__all__ = ['NestObject', 'NestObj', 'NestProperty', 'TemperatureProperty', 'NestObjectDict']
log = logging.getLogger(__name__)

NestObjectDict = dict[str, str | int | None | dict[str, Any]]
NestObj = TypeVar('NestObj', bound='NestObject')
_NotSet = object()


class NestObject(ClearableCachedPropertyMixin):
    __lock = RLock()
    __instances = {}
    type: str | None = None
    parent_type: str | None = None
    child_types: dict[str, bool] | None = None
    sub_type_key: str | None = None
    _type_cls_map: dict[str, Type[NestObj]] = {}
    _sub_type_cls_map: dict[str, dict[str, Type[NestObj]]] = {}

    # noinspection PyMethodOverriding
    def __init_subclass__(cls, type: str, parent_type: str = None, key: str = None):  # noqa
        cls.type = type
        cls.parent_type = parent_type
        cls.sub_type_key = key
        if key:
            cls._sub_type_cls_map.setdefault(type, {})[key] = cls
        else:
            cls._type_cls_map[type] = cls
        if child_types := BUCKET_CHILD_TYPES.get(type):
            cls.child_types = child_types

    def __new__(
        cls, key: str, timestamp: int | None, revision: int | None, value: dict[str, Any], *args, **kwargs
    ):
        if cls is NestObject:
            bucket_type = key.split('.', 1)[0]
            cls = cls._type_cls_map.get(bucket_type, cls)
        if key_sub_cls_map := cls._sub_type_cls_map.get(cls.type):
            for key, sub_cls in key_sub_cls_map.items():
                if key in value:
                    cls = sub_cls
                    break
        with NestObject.__lock:
            try:
                return NestObject.__instances[key]
            except KeyError:
                NestObject.__instances[key] = obj = super().__new__(cls)
                return obj

    def __init__(
        self,
        key: str,
        timestamp: int | None,
        revision: int | None,
        value: dict[str, Any],
        client: NestWebClient,
    ):
        if hasattr(self, 'key'):
            self.clear_cached_properties()
        self.key = key
        self.type, self.serial = key.split('.', 1)
        if self.parent_type is None and self.type != self.__class__.type:
            if '-' in self.serial:
                self.parent_type = 'structure'
            else:
                try:
                    int(self.serial)
                except ValueError:
                    self.parent_type = 'device'
                else:
                    self.parent_type = 'user'

        self.timestamp = timestamp
        self.revision = revision
        self.value = value
        self.client = client
        self.config = client.config
        self._refreshed = datetime.now()
        self._needs_update = False

    def __repr__(self) -> str:
        if self.__class__.type:
            return f'<{self.__class__.__name__}[{self.serial}]>'
        else:
            return f'<{self.__class__.__name__}[{self.serial}, type={self.type}]>'

    def to_dict(self) -> NestObjectDict:
        return {
            'object_key': self.key,
            'object_timestamp': self.timestamp,
            'object_revision': self.revision,
            'value': self.value,
        }

    @classmethod
    def from_dict(cls: Type[NestObj], obj: NestObjectDict, client: NestWebClient) -> NestObj:
        return cls(obj['object_key'], obj['object_timestamp'], obj['object_revision'], obj['value'], client)

    @classmethod
    async def find(
        cls: Type[NestObj], client: NestWebClient, serial: str = None, type: str = None  # noqa
    ) -> NestObj:
        if type and cls.type is not None and type != cls.type:
            expected = cls._type_cls_map.get(type, NestObject).__name__
            raise ValueError(f'Use {expected} - {cls.__name__} is incompatible with {type=}')
        return await client.get_object(type or cls.type, serial, _sub_type_key=cls.sub_type_key)

    @classmethod
    async def find_all(cls: Type[NestObj], client: NestWebClient, type: str = None) -> dict[str, NestObj]:  # noqa
        if type and cls.type is not None and type != cls.type:
            expected = cls._type_cls_map.get(type, NestObject).__name__
            raise ValueError(f'Use {expected} - {cls.__name__} is incompatible with {type=}')
        obj_dict = await client.get_objects([type or cls.type])
        if sub_type_key := cls.sub_type_key:
            return {key: obj for key, obj in obj_dict.items() if obj.sub_type_key == sub_type_key}
        else:
            return obj_dict

    # region Refresh Status Methods

    def needs_refresh(self, interval: float) -> bool:
        return self._needs_update or (datetime.now() - self._refreshed).total_seconds() >= interval

    def subscribe_dict(self, meta: bool = True) -> dict[str, str | int | None]:
        if meta:
            return {'object_key': self.key, 'object_timestamp': self.timestamp, 'object_revision': self.revision}
        else:
            return {'object_key': self.key}

    async def refresh(
        self, all: bool = True, subscribe: bool = True, send_meta: bool = True, timeout: float = None  # noqa
    ):
        last = self._refreshed
        if all:
            await self.client.refresh_known_objects(subscribe, send_meta, timeout)
        else:
            await self.client.refresh_objects([self], subscribe, send_meta, timeout=timeout)
        if last == self._refreshed:
            target = 'all objects' if all else self
            log.debug(f'Attempted to refresh {target}, but no fresh data was received for {self}')

    def _maybe_refresh(self, objects: list[NestObjectDict], source: str):
        for obj in objects:
            if obj['object_key'] == self.key:
                self._refresh(obj)
                break
        else:
            keys = [obj['object_key'] for obj in objects]
            log.warning(f'Could not refresh {self} via {source} - received unexpected {keys=}')

    def _refresh(self, obj_dict: NestObjectDict):
        log.debug(f'Received update for {self}')
        self.clear_cached_properties()
        self.revision = obj_dict['object_revision']
        self.timestamp = obj_dict['object_timestamp']
        self.value = obj_dict['value']
        self._refreshed = datetime.now()
        self._needs_update = False

    async def _subscribe(self, send_meta: bool = False):
        self._maybe_refresh(await self.client.subscribe([self], send_meta), 'subscribe')

    async def _app_launch(self):
        self._maybe_refresh(await self.client.get_buckets([self.type]), 'app_launch')

    # endregion

    async def _set_key(self, key: str, value: Any, op: str = 'MERGE') -> Response:
        return await self._set_full({key: value}, op)

    async def _set_full(self, data: dict[str, Any], op: str = 'MERGE') -> Response:
        payload = {'objects': [{'object_key': self.key, 'op': op, 'value': data}]}
        self._needs_update = True
        async with self.client.transport_url() as client:
            log.debug(f'Submitting {payload=}')
            self.clear_cached_properties()
            return await client.post('v5/put', json=payload)

    # region Parent/Child Object Methods

    def is_child_of(self, nest_obj: NestObj) -> bool:
        return nest_obj.is_parent_of(self)

    def is_parent_of(self, nest_obj: NestObj) -> bool:
        return self.child_types and nest_obj.type in self.child_types and nest_obj.serial == self.serial

    @cached_classproperty
    def fetch_child_types(cls) -> tuple[str, ...]:  # noqa
        if child_types := cls.child_types:
            return tuple(t for t, fetch in child_types.items() if fetch)
        return ()

    async def get_children(self) -> dict[str, NestObj]:
        """Mapping of {type: NestObject} for this object's children"""
        if fetch_child_types := self.fetch_child_types:
            key_obj_map = await self.client.get_objects(fetch_child_types)
            return {obj.type: obj for obj in key_obj_map.values() if obj.serial == self.serial}
        return {}

    async def get_parent(self) -> NestObj | None:
        if self.parent_type:
            try:
                return await self.client.get_object(self.parent_type, self.serial)
            except NestObjectNotFound:
                return None
        return None

    # endregion


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
        attr_path = ''.join(f'[{p!r}]' for p in self.path)
        self.__doc__ = (
            f'A :class:`NestProperty<nest.entities.base.NestProperty>` that references this {owner.__name__}'
            f' instance\'s {self.attr}{attr_path}'
        )

    def __get__(self, obj: NestObject, cls):
        if obj is None:
            return self

        # TODO: Fix update/refresh handling
        # if obj._needs_update:
        #     await obj.refresh()

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

    # def __get__(self, obj: NestObject, cls):
    #     if obj is None:
    #         return self
    #     return self._get(obj).__await__()


class TemperatureProperty(NestProperty):
    def __get__(self, obj: NestObject, cls):
        if obj is None:
            return self
        value_c = super().__get__(obj, cls)
        if obj.client.config.temp_unit == 'f':
            return celsius_to_fahrenheit(value_c)
        return value_c
