"""
Classes that represent Nest Structures, Users, Devices/Thermostats, etc.

:author: Doug Skrypa
"""

import logging
from typing import TYPE_CHECKING, Any

from async_property import async_cached_property

from .base import NestObject, NestProperty
from .device import Device, ThermostatDevice

if TYPE_CHECKING:
    from .user import User

__all__ = ['Structure']
log = logging.getLogger(__name__)


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

    @async_cached_property
    async def members(self) -> dict['User', dict[str, Any]]:
        return {await self.client.get_user(member['user']): member for member in self.value['members']}

    @async_cached_property
    async def user(self) -> 'User':
        return await self.client.get_user(self.value['user'])

    @async_cached_property
    async def swarm(self) -> dict[str, 'Device']:
        dev_keys = set(self.value['swarm'])
        return {dev_key: dev for dev_key, dev in (await self.client.get_devices()).items() if dev_key in dev_keys}

    @async_cached_property
    async def devices(self) -> dict[str, 'Device']:
        dev_keys = set(self.value['devices'])
        return {dev_key: dev for dev_key, dev in (await self.client.get_devices()).items() if dev_key in dev_keys}

    @async_cached_property
    async def thermostats(self) -> tuple['ThermostatDevice']:
        return tuple(dev for dev in (await self.devices).values() if isinstance(dev, ThermostatDevice))

    async def set_away(self, away: bool):
        await self._set_key('away', away)
