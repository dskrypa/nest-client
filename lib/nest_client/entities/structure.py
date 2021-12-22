"""
Classes that represent Nest Structures, Users, Devices/Thermostats, etc.

:author: Doug Skrypa
"""

import logging
from asyncio import gather
from typing import TYPE_CHECKING, Any, Optional

from .base import NestObject, NestProperty
from .device import Device, ThermostatDevice, Shared

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
    device_ids = NestProperty('devices', type=set)  # type: set[str]

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__}[{self.serial}, name={self.name!r}, location={self.location!r}]>'

    async def get_members(self) -> dict['User', dict[str, Any]]:
        users = await self.client.get_users()
        return {users[member['user']]: member for member in self.value['members']}

    async def get_user(self) -> 'User':
        return await self.client.get_user(self.value['user'])

    async def get_swarm(self) -> dict[str, 'Device']:
        dev_keys = set(self.value['swarm'])
        devices = await self.client.get_devices()
        return {dev_key: dev for dev_key, dev in devices.items() if dev_key in dev_keys}

    async def get_devices(self) -> dict[str, 'Device']:
        dev_keys = set(self.value['devices'])
        devices = await self.client.get_devices()
        return {dev_key: dev for dev_key, dev in devices.items() if dev_key in dev_keys}

    async def devices_and_shared(self) -> dict[str, tuple['Device', Optional['Shared']]]:
        dev_keys = set(self.value['devices'])
        devices = await self.client.get_devices()
        filtered = [dev for dev_key, dev in devices.items() if dev_key in dev_keys]
        dev_shared_tuples = await gather(*(dev.dev_shared_tuple() for dev in filtered))
        return {dev.key: (dev, shared) for dev, shared in dev_shared_tuples}

    async def get_thermostats(self) -> tuple['ThermostatDevice']:
        devices = await self.get_devices()
        return tuple(dev for dev in devices.values() if isinstance(dev, ThermostatDevice))

    async def thermostats_and_shared(self) -> tuple[tuple['ThermostatDevice', Optional['Shared']]]:
        devices_and_shared = await self.devices_and_shared()
        return tuple((dev, shared) for dev, shared in devices_and_shared.values() if isinstance(dev, ThermostatDevice))

    async def set_away(self, away: bool):
        await self._set_key('away', away)
