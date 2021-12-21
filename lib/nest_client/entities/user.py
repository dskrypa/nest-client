"""
Classes that represent Nest Structures, Users, Devices/Thermostats, etc.

:author: Doug Skrypa
"""

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Union

from async_property import async_cached_property

from .base import NestObject, NestProperty

if TYPE_CHECKING:
    from .structure import Structure

__all__ = ['User', 'Messages', 'Buckets']
log = logging.getLogger(__name__)


class User(NestObject, type='user', parent_type=None):
    name = NestProperty('name')
    email = NestProperty('email')

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__}[{self.serial}, name={self.name!r}]>'

    @async_cached_property
    async def structures(self) -> dict[str, 'Structure']:
        return {did: (await self.client.objects)[did] for did in self.value['structures']}

    @async_cached_property
    async def structure_memberships(self) -> dict['Structure', dict[str, Any]]:
        members = {}
        for member in self.value['structure_memberships']:
            user = (await self.client.objects)[member['structure']]
            members[user] = member
        return members  # noqa


class Messages(NestObject, type='message_center', parent_type='user'):
    messages = NestProperty('messages')  # type: list[dict[str, Union[int, bool, str, list[str]]]]
    # Message keys: thread_id, read, priority, timestamp, dismisses, key, id, parameters


class Buckets(NestObject, type='buckets', parent_type='user'):
    async def types_by_parent(self) -> dict[NestObject, set[str]]:
        types = defaultdict(set)
        for bucket in self.value['buckets']:
            bucket_type, serial = bucket.split('.', 1)
            parent = (await self.client.parent_objects)[serial]
            types[parent].add(bucket_type)
        return types
