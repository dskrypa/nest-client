"""
Classes that represent Nest Structures, Users, Devices/Thermostats, etc.

:author: Doug Skrypa
"""

import logging
import time
from functools import cached_property
from typing import TYPE_CHECKING, Any, Union, Optional, TypeVar

from tz_aware_dt.utils import format_duration

from ..constants import TARGET_TEMP_TYPES, NEST_WHERE_MAP, ALLOWED_TEMPS
from ..utils import fahrenheit_to_celsius as f2c
from .base import NestObject, NestProperty, TemperatureProperty

if TYPE_CHECKING:
    from requests import Response
    from .structure import Structure

__all__ = ['Device', 'Shared', 'EnergyUsage', 'NestDevice']
log = logging.getLogger(__name__)

NestDevice = TypeVar('NestDevice', bound='Device')


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
    def structure(self) -> Optional['Structure']:
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
    name = NestProperty('name')  # type: str
    mode = NestProperty('target_temperature_type')  # type: str  # one of: TARGET_TEMP_TYPES
    target_temperature_type = NestProperty('target_temperature_type')  # type: str  # one of: TARGET_TEMP_TYPES
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
    hvac_ac_state = NestProperty('hvac_ac_state')  # type: bool
    hvac_heater_state = NestProperty('hvac_heater_state')  # type: bool
    hvac_fan_state = NestProperty('hvac_fan_state')  # type: bool

    @property
    def hvac_state(self) -> str:
        if self.hvac_ac_state:
            return 'cooling'
        elif self.hvac_heater_state:
            return 'heating'
        elif self.hvac_fan_state:
            return 'fan running'
        return 'off'

    @property
    def running(self) -> bool:
        return self.hvac_fan_state or self.hvac_ac_state or self.hvac_heater_state

    @cached_property
    def allowed_temp_range(self) -> tuple[int, int]:
        return ALLOWED_TEMPS[self.config.temp_unit]

    @property
    def target_temp_range(self) -> tuple[float, float]:
        return self.target_temperature_low, self.target_temperature_high

    def set_temp_range(self, low: float, high: float) -> 'Response':
        """
        :param low: Minimum temperature to maintain in Celsius (heat will turn on if the temp drops below this)
        :param high: Maximum temperature to allow in Celsius (air conditioning will turn on above this)
        :return: The raw response
        """
        if self.config.temp_unit == 'f':
            low = f2c(low)
            high = f2c(high)
        return self._set_full({'target_temperature_low': low, 'target_temperature_high': high})

    def set_temp(self, temp: float, temporary: bool = False, convert: bool = True) -> 'Response':
        if convert and self.config.temp_unit == 'f':
            temp = f2c(temp)
        adj = 'temporary' if temporary else 'requested'
        log.debug(f'Setting {adj} temp={temp:.1f}')
        return self._set_key('target_temperature', temp)

    def set_temp_and_force_run(self, temp: float) -> 'Response':
        if fahrenheit := self.config.temp_unit == 'f':
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
