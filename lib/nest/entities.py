"""
Classes that represent Nest Structures, Users, Devices/Thermostats, etc.

:author: Doug Skrypa
"""

import logging
import time
from collections import defaultdict
from datetime import datetime
from functools import cached_property
from threading import RLock
from typing import TYPE_CHECKING, Any, Union, Optional, TypeVar, Type

from tz_aware_dt.utils import format_duration

from .constants import TARGET_TEMP_TYPES, BUCKET_CHILD_TYPES, NEST_WHERE_MAP
from .exceptions import NestObjectNotFound
from .utils import NestProperty, TemperatureProperty, ClearableCachedPropertyMixin, fahrenheit_to_celsius as f2c
from .utils import cached_classproperty

if TYPE_CHECKING:
    from requests import Response
    from .client import NestWebClient

__all__ = ['NestObject', 'Structure', 'User', 'Device', 'Shared', 'Schedule', 'EnergyUsage', 'NestObj', 'NestDevice']
log = logging.getLogger(__name__)

NestObjectDict = dict[str, Union[str, int, None, dict[str, Any]]]
NestObj = TypeVar('NestObj', bound='NestObject')
NestDevice = TypeVar('NestDevice', bound='Device')


class NestObject(ClearableCachedPropertyMixin):
    __lock = RLock()
    __instances = {}
    type: Optional[str] = None
    parent_type: Optional[str] = None
    child_types: Optional[dict[str, bool]] = None
    _type_cls_map: dict[str, Type[NestObj]] = {}
    _sub_type_cls_map: dict[str, dict[str, Type[NestObj]]] = {}

    # noinspection PyMethodOverriding
    def __init_subclass__(cls, type: str, parent_type: str = None, key: str = None):  # noqa
        cls.type = type
        cls.parent_type = parent_type
        if key:
            cls._sub_type_cls_map.setdefault(type, {})[key] = cls
        else:
            cls._type_cls_map[type] = cls
        if child_types := BUCKET_CHILD_TYPES.get(type):
            cls.child_types = child_types

    def __new__(
        cls, key: str, timestamp: Optional[int], revision: Optional[int], value: dict[str, Any], *args, **kwargs
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
        timestamp: Optional[int],
        revision: Optional[int],
        value: dict[str, Any],
        client: 'NestWebClient',
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
        self._refreshed = datetime.now()

    def __repr__(self) -> str:
        if self.__class__.type:
            return f'<{self.__class__.__name__}[{self.serial}]>'
        else:
            return f'<{self.__class__.__name__}[{self.serial}, type={self.type}]>'

    @classmethod
    def from_dict(cls: Type[NestObj], obj: NestObjectDict, client: 'NestWebClient') -> NestObj:
        return cls(obj['object_key'], obj['object_timestamp'], obj['object_revision'], obj['value'], client)

    @classmethod
    def find(cls: Type[NestObj], client: 'NestWebClient', serial: str = None, type: str = None) -> NestObj:  # noqa
        if type and cls.type is not None and type != cls.type:
            expected = cls._type_cls_map.get(type, NestObject).__name__
            raise ValueError(f'Use {expected} - {cls.__name__} is incompatible with {type=}')
        return client.get_object(type or cls.type, serial)

    @classmethod
    def find_all(cls: Type[NestObj], client: 'NestWebClient', type: str = None) -> dict[str, NestObj]:  # noqa
        if type and cls.type is not None and type != cls.type:
            expected = cls._type_cls_map.get(type, NestObject).__name__
            raise ValueError(f'Use {expected} - {cls.__name__} is incompatible with {type=}')
        return client.get_objects([type or cls.type])

    # region Refresh Status Methods

    def subscribe_dict(self, meta: bool = True) -> dict[str, Union[str, int, None]]:
        if meta:
            return {'object_key': self.key, 'object_timestamp': self.timestamp, 'object_revision': self.revision}
        else:
            return {'object_key': self.key}

    def refresh(self, all: bool = True, subscribe: bool = True, send_meta: bool = True):  # noqa
        last = self._refreshed
        if all:
            self.client.refresh_known_objects(subscribe, send_meta)
        else:
            self.client.refresh_objects([self], subscribe, send_meta)
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
        self.clear_cached_properties()
        self.revision = obj_dict['object_revision']
        self.timestamp = obj_dict['object_timestamp']
        self.value = obj_dict['value']
        self._refreshed = datetime.now()

    def _subscribe(self, send_meta: bool = False):
        self._maybe_refresh(self.client.subscribe([self], send_meta), 'subscribe')

    def _app_launch(self):
        self._maybe_refresh(self.client.get_buckets([self.type]), 'app_launch')

    # endregion

    def _set_key(self, key: str, value: Any, op: str = 'MERGE') -> 'Response':
        return self._set_full({key: value}, op)

    def _set_full(self, data: dict[str, Any], op: str = 'MERGE') -> 'Response':
        payload = {'objects': [{'object_key': self.key, 'op': op, 'value': data}]}
        with self.client.transport_url() as client:
            log.debug(f'Submitting {payload=}')
            return client.post('v5/put', json=payload)

    # region Parent/Child Object Methods

    def is_child_of(self, nest_obj: NestObj) -> bool:
        return nest_obj.is_parent_of(self)

    def is_parent_of(self, nest_obj: NestObj) -> bool:
        return self.child_types and nest_obj.type in self.child_types and nest_obj.serial == self.serial

    @cached_classproperty
    def fetch_child_types(cls) -> tuple[str, ...]:
        if child_types := cls.child_types:
            return tuple(t for t, fetch in child_types.items() if fetch)
        return ()

    @cached_property
    def children(self) -> dict[str, NestObj]:
        """Mapping of {type: NestObject} for this object's children"""
        if fetch_child_types := self.fetch_child_types:
            key_obj_map = self.client.get_objects(fetch_child_types)
            return {obj.type: obj for obj in key_obj_map.values() if obj.serial == self.serial}
        return {}

    @cached_property
    def parent(self) -> Optional[NestObj]:
        if self.parent_type:
            try:
                return self.client.get_object(self.parent_type, self.serial)
            except NestObjectNotFound:
                return None
        return None

    # endregion


class Structure(NestObject, type='structure', parent_type=None):
    name = NestProperty('name')
    location = NestProperty('location')
    country_code = NestProperty('country_code')
    postal_code = NestProperty('postal_code')
    time_zone = NestProperty('time_zone')
    house_type = NestProperty('house_type')
    away = NestProperty('away')  # type: bool
    away_timestamp = NestProperty('away_timestamp')  # type: int

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__}[{self.serial}, name={self.name!r}, location={self.location!r}]>'

    @cached_property
    def members(self) -> dict['User', dict[str, Any]]:
        return {self.client.get_user(member['user']): member for member in self.value['members']}

    @cached_property
    def user(self) -> 'User':
        return self.client.get_user(self.value['user'])

    @cached_property
    def swarm(self) -> dict[str, 'Device']:
        dev_keys = set(self.value['swarm'])
        return {dev_key: dev for dev_key, dev in self.client.get_devices().items() if dev_key in dev_keys}

    @cached_property
    def devices(self) -> dict[str, 'Device']:
        dev_keys = set(self.value['devices'])
        return {dev_key: dev for dev_key, dev in self.client.get_devices().items() if dev_key in dev_keys}

    @cached_property
    def thermostats(self) -> tuple['ThermostatDevice']:
        return tuple(dev for dev in self.devices.values() if isinstance(dev, ThermostatDevice))


class User(NestObject, type='user', parent_type=None):
    name = NestProperty('name')
    email = NestProperty('email')

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__}[{self.serial}, name={self.name!r}]>'

    @cached_property
    def structures(self) -> dict[str, 'Structure']:
        return {did: self.client.objects[did] for did in self.value['structures']}

    @cached_property
    def structure_memberships(self) -> dict['Structure', dict[str, Any]]:
        members = {}
        for member in self.value['structure_memberships']:
            user = self.client.objects[member['structure']]
            members[user] = member
        return members  # noqa


class Device(NestObject, type='device', parent_type=None):
    name = NestProperty('name', default='')
    device_id = NestProperty('weave_device_id')
    software_version = NestProperty('current_version')
    model_version = NestProperty('model_version')
    postal_code = NestProperty('postal_code')
    where_id = NestProperty('where_id', default=None)

    is_thermostat: bool = False
    is_camera: bool = False
    is_smoke_co_alarm: bool = False

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__}[{self.serial}, name={self.name!r}, model={self.model_version!r}]>'

    @cached_property
    def structure(self) -> Optional[Structure]:
        return next((st for st in self.client.get_structures().values() if self.key in st.devices), None)

    @cached_property
    def shared(self) -> Optional['Shared']:
        return self.children.get('shared')

    @cached_property
    def where(self) -> Optional[str]:
        return NEST_WHERE_MAP.get(self.where_id)

    @cached_property
    def description(self) -> str:
        name, where = self.name, self.where
        return f'{name} - {where}' if name and where else name if name else where if where else ''


class ThermostatDevice(Device, type='device', parent_type=None, key='hvac_wires'):
    backplate_model = NestProperty('backplate_model')
    backplate_serial = NestProperty('backplate_serial_number')
    _backplate_temperature = NestProperty('backplate_temperature')  # type: float  # celsius
    backplate_temperature = TemperatureProperty('backplate_temperature')  # type: float  # unit from config
    capability_level = NestProperty('capability_level')

    battery_level = NestProperty('battery_level')
    schedule_mode = NestProperty('current_schedule_mode')

    humidity = NestProperty('current_humidity')
    fan_current_speed = NestProperty('fan_current_speed')

    is_thermostat: bool = True

    @cached_property
    def has(self) -> dict[str, bool]:
        return {k[4:]: v for k, v in self.value.items() if k.startswith('has_')}

    @cached_property
    def fan(self) -> dict[str, Union[str, bool, int]]:
        return {k[4:]: v for k, v in self.value.items() if k.startswith('fan_')}

    def start_fan(self, duration: int = 1800) -> 'Response':
        """
        :param duration: Number of seconds for which the fan should run
        :return: The raw response
        """
        timeout = int(time.time()) + duration
        log.debug(f'Submitting fan start request with duration={format_duration(duration)} => end time of {timeout}')
        return self._set_key('fan_timer_timeout', timeout)

    def stop_fan(self) -> 'Response':
        return self._set_key('fan_timer_timeout', 0)


class Shared(NestObject, type='shared', parent_type='device'):
    name = NestProperty('name')
    mode = NestProperty('target_temperature_type')  # one of: TARGET_TEMP_TYPES
    target_temperature_type = NestProperty('target_temperature_type')  # one of: TARGET_TEMP_TYPES
    _target_temperature_high = NestProperty('target_temperature_high')  # type: float  # celsius
    _target_temperature_low = NestProperty('target_temperature_low')  # type: float  # celsius
    _target_temperature = NestProperty('target_temperature')  # type: float  # celsius
    _current_temperature = NestProperty('current_temperature')  # type: float  # celsius
    target_temperature_high = TemperatureProperty('target_temperature_high')  # type: float  # unit from config
    target_temperature_low = TemperatureProperty('target_temperature_low')  # type: float  # unit from config
    target_temperature = TemperatureProperty('target_temperature')  # type: float  # unit from config
    current_temperature = TemperatureProperty('current_temperature')  # type: float  # unit from config
    can_heat = NestProperty('can_heat')  # type: bool
    can_cool = NestProperty('can_cool')  # type: bool
    compressor_lockout_timeout = NestProperty('compressor_lockout_timeout')
    compressor_lockout_enabled = NestProperty('compressor_lockout_enabled')
    hvac_ac_state = NestProperty('hvac_ac_state')
    hvac_heater_state = NestProperty('hvac_heater_state')
    hvac_fan_state = NestProperty('hvac_fan_state')

    def set_temp_range(self, low: float, high: float) -> 'Response':
        """
        :param low: Minimum temperature to maintain in Celsius (heat will turn on if the temp drops below this)
        :param high: Maximum temperature to allow in Celsius (air conditioning will turn on above this)
        :return: The raw response
        """
        if self.client.config.temp_unit == 'f':
            low = f2c(low)
            high = f2c(high)
        return self._set_full({'target_temperature_low': low, 'target_temperature_high': high})

    def set_temp(self, temp: float, temporary: bool = False, convert: bool = True) -> 'Response':
        if convert and self.client.config.temp_unit == 'f':
            temp = f2c(temp)
        adj = 'temporary' if temporary else 'requested'
        log.debug(f'Setting {adj} temp={temp:.1f}')
        return self._set_key('target_temperature', temp)

    def set_temp_and_force_run(self, temp: float) -> 'Response':
        if fahrenheit := self.client.config.temp_unit == 'f':
            temp = f2c(temp)
        mode = self.mode.upper()
        current = self._current_temperature
        if mode == 'COOL':
            delta = current - temp
            if current > temp and delta < 0.5:
                tmp = current - 0.6
                self.set_temp(tmp, True, False)
                time.sleep(3)
        elif mode == 'HEAT':
            delta = temp - current
            log.debug(f'{current=} {temp=} {delta=} {fahrenheit=}')
            if current < temp and delta < 0.5:
                tmp = current + 0.6
                self.set_temp(tmp, True, False)
                time.sleep(3)
        else:
            log.log(19, f'Unable to force unit to run for {mode=!r}')
        return self.set_temp(temp, convert=False)

    def set_mode(self, mode: str) -> 'Response':
        """
        :param mode: One of 'cool', 'heat', 'range', or 'off'
        :return: The raw response
        """
        if mode not in TARGET_TEMP_TYPES:
            raise ValueError(f'Invalid {mode=}')
        return self._set_key('target_temperature_type', mode)


class Schedule(NestObject, type='schedule', parent_type='device'):
    name = NestProperty('name')
    version = NestProperty('ver')  # type: int
    mode = NestProperty('schedule_mode')
    days = NestProperty('days')  # type: dict[str, dict[str, dict[str, Union[str, int, float]]]]
    where_id = NestProperty('where_id')

    @cached_property
    def where(self) -> str:
        return NEST_WHERE_MAP.get(self.where_id, self.where_id)


class EnergyUsage(NestObject, type='energy_latest', parent_type='device'):
    recent_max_used = NestProperty('recent_max_used')  # type: int

    @cached_property
    def days(self) -> dict[str, 'EnergyUsageDay']:
        return {d['day']: EnergyUsageDay(self, d) for d in self.value['days']}


class EnergyUsageDay:
    day = NestProperty('day')  # type: str
    device_timezone_offset = NestProperty('device_timezone_offset')  # type: int
    total_heating_time = NestProperty('total_heating_time')  # type: int
    total_cooling_time = NestProperty('total_cooling_time')  # type: int
    total_fan_cooling_time = NestProperty('total_fan_cooling_time')  # type: int
    total_humidifier_time = NestProperty('total_humidifier_time')  # type: int
    total_dehumidifier_time = NestProperty('total_dehumidifier_time')  # type: int
    leafs = NestProperty('leafs')  # type: int
    whodunit = NestProperty('whodunit')  # type: int
    recent_avg_used = NestProperty('recent_avg_used')  # type: int
    usage_over_avg = NestProperty('usage_over_avg')  # type: int
    cycles = NestProperty('cycles')  # type: list[dict[str, int]]
    events = NestProperty('events')  # type: list[dict[str, Union[str, int, float, bool]]]
    rates = NestProperty('rates')  # type: list[dict[str, Any]]
    system_capabilities = NestProperty('system_capabilities')  # type: int
    incomplete_fields = NestProperty('incomplete_fields')  # type: int

    def __init__(self, parent: 'EnergyUsage', value: dict[str, Any]):
        self.parent = parent
        self.value = value


class Messages(NestObject, type='message_center', parent_type='user'):
    messages = NestProperty('messages')  # type: list[dict[str, Union[int, bool,  str, list[str]]]]
    # Message keys: thread_id, read, priority, timestamp, dismisses, key, id, parameters


class Buckets(NestObject, type='buckets', parent_type='user'):
    def types_by_parent(self) -> dict[NestObject, set[str]]:
        types = defaultdict(set)
        for bucket in self.value['buckets']:
            bucket_type, serial = bucket.split('.', 1)
            parent = self.client.parent_objects[serial]
            types[parent].add(bucket_type)
        return types
