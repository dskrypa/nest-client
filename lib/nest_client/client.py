"""
Library for interacting with the Nest thermostat via the web API

:author: Doug Skrypa
"""

import json
import logging
import pickle
import time
from asyncio import Lock, get_running_loop, gather
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Union, Optional, Mapping, Iterable, Any, AsyncContextManager
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from httpx import HTTPError, TimeoutException, HTTPStatusError

from requests_client.async_client import AsyncRequestsClient
from requests_client.user_agent import USER_AGENT_CHROME

from .utils import get_user_cache_dir
from .config import NestConfig
from .constants import JWT_URL, NEST_API_KEY, NEST_URL, OAUTH_URL, INIT_BUCKET_TYPES
from .exceptions import SessionExpired, ConfigError, NestObjectNotFound
from .entities.base import NestObjectDict, NestObject, NestObj
from .entities.device import NestDevice, Device, Shared
from .entities.structure import Structure
from .entities.user import User

__all__ = ['NestWebClient']
log = logging.getLogger(__name__)
UTC = ZoneInfo('UTC')


class NestWebClient:
    _nest_host_port = ('home.nest.com', None)

    def __init__(self, config_path: str = None, reauth: bool = False, overrides: Mapping[str, Optional[str]] = None):
        self.config = NestConfig(config_path, overrides)
        self._client = AsyncRequestsClient(
            NEST_URL, user_agent_fmt=USER_AGENT_CHROME, headers={'Referer': NEST_URL}, log_data=True
        )
        self.auth = NestWebAuth(self.config, self._client, reauth)
        self._known_objects: dict[str, NestObj] = {}
        self._latest_transport_url = None
        self._latest_weather = None
        self._last_known_reauth = datetime.now()
        self._user_id = None

    async def user_id(self) -> str:
        if self._user_id is None:
            self._user_id = await self.auth.user_id()
        return self._user_id

    # region URLs

    @asynccontextmanager
    async def transport_url(self) -> AsyncContextManager[AsyncRequestsClient]:
        host, port = await self._transport_host_port()  # Must be outside `with` to prevent deadlock if update is needed
        async with self.auth:
            log.debug(f'Using host:port={host}:{port}')
            self._client.host, self._client.port = host, port
            yield self._client

    @asynccontextmanager
    async def nest_url(self) -> AsyncContextManager[AsyncRequestsClient]:
        async with self.auth:
            host, port = self._nest_host_port
            log.debug(f'Using host:port={host}:{port}')
            self._client.host, self._client.port = host, port
            yield self._client

    async def _transport_host_port(self) -> tuple[str, Optional[int]]:
        if self._needs_transport_url_update():
            await self.app_launch()
        return self._latest_transport_url.hostname, self._latest_transport_url.port

    def _needs_transport_url_update(self) -> bool:
        return self._latest_transport_url is None or self.auth.last_reauth > self._last_known_reauth

    # endregion

    # region Low Level Methods

    async def app_launch(self, bucket_types: Iterable[str] = None, timeout: float = None) -> dict[str, Any]:
        bucket_types = list(bucket_types) if bucket_types else []
        payload = {'known_bucket_types': bucket_types, 'known_bucket_versions': []}
        async with self.nest_url() as client:
            user_id = await self.user_id()
            try:
                resp = await client.post(f'api/0.1/user/{user_id}/app_launch', json=payload, timeout=timeout)
            except HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    self.auth.force_reauth = True
                raise

            data = resp.json()
            if self._needs_transport_url_update():
                self._latest_weather = data['weather_for_structures']
                self._latest_transport_url = urlparse(data['service_urls']['urls']['transport_url'])
                self._last_known_reauth = self.auth.last_reauth
            return data

    async def get_buckets(self, types: Iterable[str], timeout: float = None) -> list[NestObjectDict]:
        data = await self.app_launch(types, timeout)
        return data['updated_buckets']

    async def get_mobile_info(self) -> dict[str, Any]:
        """Returns the same info as app_launch, but in a slightly different format"""
        async with self.transport_url() as client:
            user_id = await self.user_id()
            resp = await client.get(f'v2/mobile/user.{user_id}')
            return resp.json()

    async def get_weather_location(self) -> tuple[str, str]:
        if self._latest_weather is None:
            await self.app_launch()
        location = next(iter(self._latest_weather.values()))['location']
        return location['zip'], location['country']

    async def get_weather(self, zip_code: Union[str, int] = None, country_code: str = None) -> dict[str, Any]:
        """
        Get the weather forecast.  Response format::
            {
              "display_city":"...", "city":"...",
              "forecast":{
                "hourly":[{"time":1569769200, "temp":74.0, "humidity":55},...],
                "daily":[{
                  "conditions":"Sunny","date":1569729600,"high_temperature":77.0,"icon":"sunny","low_temperature":60.0
                },...]
              },
              "now":{
                "station_id":"unknown", "conditions":"Mostly Cloudy", "current_humidity":60, "current_temperature":22.8,
                "current_wind":12, "gmt_offset":"-04.00", "icon":"mostlycloudy", "sunrise":1569754260,
                "sunset":1569796920, "wind_direction":"N"
              }
            }

        :param zip_code: A 5-digit zip code
        :param country_code: A 2-letter country code (such as 'US')
        :return: The parsed response
        """
        if zip_code is None:
            zip_code, country_code = await self.get_weather_location()

        country_code = country_code or 'US'
        async with self.nest_url() as client:
            resp = await client.get(f'api/0.1/weather/forecast/{zip_code},{country_code}')
            return resp.json()

    # endregion

    # region High Level Object Methods

    async def get_objects(self, types: Iterable[str], cached: bool = True, children: bool = True) -> dict[str, NestObj]:
        types = set(types)
        if cached and (obj_dict := {k: v for k, v in self._known_objects.items() if v.type in types}):
            found_types = {obj.type for obj in obj_dict.values()}
            if missing := types.difference(found_types):
                log.debug(f'Found={found_types} requested={types} - retrieving {missing=}')
                obj_dict.update(await self.get_objects(missing, False))
            return obj_dict

        orig_types = types
        if children:
            types = _expand_with_children(types)

        log.debug(f'Requesting buckets for {types=}')
        obj_dict = {obj['object_key']: NestObject.from_dict(obj, self) for obj in (await self.get_buckets(types))}
        log.debug('Found new objects: ' + ', '.join(sorted(obj_dict)))
        self._known_objects.update(obj_dict)
        if children and orig_types != types:
            obj_dict = {key: obj for key, obj in obj_dict.items() if obj.type in orig_types}
        return obj_dict

    async def get_object(
        self,
        type: str,  # noqa
        serial: str = None,
        cached: bool = True,
        children: bool = True,
        _sub_type_key: str = None,
    ) -> NestObj:
        if cached:
            try:
                return self._get_object(self._known_objects, type, serial)
            except NestObjectNotFound:  # let ValueError propagate
                log.debug(f'Did not find cached object with {type=} {serial=}')
                pass  # try fresh objects
        return self._get_object(await self.get_objects([type], False, children), type, serial, _sub_type_key)

    def _get_object(
        self, obj_dict: dict[str, NestObj], type: str, serial: str = None, _sub_type_key: str = None  # noqa
    ) -> NestObj:
        if not serial and (type == 'device' or type in Device.child_types):
            serial = self.config.serial
        if serial:
            object_key = f'{type}.{serial}'
            try:
                obj = obj_dict[object_key]
            except KeyError:
                raise NestObjectNotFound(f'Could not find {object_key=} (found={list(obj_dict)})')
            else:
                if _sub_type_key and _sub_type_key not in obj.value:
                    raise ValueError(f'Invalid {serial=} for {_type_not_found_description(type, _sub_type_key)}')
                else:
                    return obj
        else:
            if _sub_type_key:
                obj_dict = {key: obj for key, obj in obj_dict.items() if obj.sub_type_key == _sub_type_key}

            if (ko_count := len(obj_dict)) == 1:
                return next(iter(obj_dict.values()))
            else:
                desc = _type_not_found_description(type, _sub_type_key)
                if not obj_dict:
                    raise NestObjectNotFound(f'No {desc} objects were found from {self}')
                else:
                    raise ValueError(f'A serial number is required - found {ko_count} {desc} objects: {list(obj_dict)}')

    async def get_init_objects(self, cached: bool = False) -> dict[str, NestObj]:
        return await self.get_objects(INIT_BUCKET_TYPES, cached)

    async def get_init_parent_objects(self, cached: bool = True) -> dict[str, NestObj]:
        init_objects = await self.get_init_objects(cached)
        return {obj.serial: obj for obj in init_objects.values() if obj.parent_type is None}

    # endregion

    # region Typed NestObject Getters

    async def get_device(self, serial: str = None, cached: bool = True, children: bool = True) -> NestDevice:
        return await self.get_object('device', serial, cached, children)

    async def get_devices(self, cached: bool = True, children: bool = True) -> dict[str, NestDevice]:
        return await self.get_objects(['device'], cached, children)

    async def get_structure(self, serial: str = None, cached: bool = True) -> Structure:
        return await self.get_object('structure', serial, cached)

    async def get_structures(self, cached: bool = True) -> dict[str, Structure]:
        return await self.get_objects(['structure'], cached)

    async def get_user(self, serial: str = None, cached: bool = True) -> User:
        return await self.get_object('user', serial, cached)

    async def get_users(self, cached: bool = True) -> dict[str, User]:
        return await self.get_objects(['user'], cached)

    async def get_shared(self, serial: str = None, cached: bool = True) -> Shared:
        return await self.get_object('shared', serial, cached)

    async def get_shareds(self, cached: bool = True) -> dict[str, Shared]:
        return await self.get_objects(['shared'], cached)

    # endregion

    # region Refresh Methods

    async def subscribe(
        self, objects: Iterable[NestObj], send_meta: bool = True, timeout: float = 5
    ) -> list[NestObjectDict]:
        payload = {'objects': [obj.subscribe_dict(send_meta) for obj in objects], 'timeout': 863}
        log.debug(f'Submitting subscribe request with {payload=}')
        async with self.transport_url() as client:
            resp = await client.post('v5/subscribe', json=payload, timeout=timeout)
            return resp.json()['objects']

    async def refresh_known_objects(self, subscribe: bool = True, send_meta: bool = True, timeout: float = None):
        await self.refresh_objects(self._known_objects.values(), subscribe, send_meta, timeout=timeout)

    async def refresh_objects(
        self,
        objects: Iterable[NestObj],
        subscribe: bool = True,
        send_meta: bool = True,
        *,
        timeout: float = None,
        children: bool = True,
    ):
        try:
            if subscribe:
                objects = set(objects)
                if children:
                    children_groups = await gather(*(obj.get_children() for obj in objects))
                    objects.update(c for group in children_groups for c in group.values())
                raw_objs = await self.subscribe(objects, send_meta, timeout or 5)
            else:
                types = {obj.type for obj in objects}
                raw_objs = await self.get_buckets(_expand_with_children(types) if children else types, timeout=timeout)
        except TimeoutException:
            log.debug('Refresh subscribe request timed out')
        except HTTPStatusError as e:
            resp = e.response
            if (code := resp.status_code) == 429:
                retry_after = resp.headers.get('Retry-After')
                reason = f'rate limit - {retry_after=}'
            elif code in (401, 403):
                reason = f'unauthorized ({code})'
                self.auth.force_reauth = True
            else:
                reason = f'error [{code}]'
            log.debug(f'Refresh failed due to {reason}: {e}')
        except HTTPError as e:
            log.debug(f'Refresh failed due to error: {e}')
        else:
            for raw_obj in raw_objs:
                key = raw_obj['object_key']
                if obj := self._known_objects.get(key):
                    obj._refresh(raw_obj)
                else:
                    self._known_objects[key] = obj = NestObject.from_dict(raw_obj, self)
                    log.debug(f'Found new {obj=} during refresh')

    # endregion

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._client.aclose()


class NestWebAuth:
    def __init__(self, config: NestConfig, client: AsyncRequestsClient, force_reauth: bool = False):
        self._client = client
        self.config = config
        if 'oauth' not in self.config:
            raise ConfigError(self.config, 'Missing required oauth configs')
        self.cache_path = get_user_cache_dir('nest').joinpath('session.pickle')
        self.force_reauth = force_reauth
        self.last_reauth = None
        self.expiry = None
        self._lock = Lock()
        self._user_id = None

    async def user_id(self) -> str:
        if self._user_id is None:
            await self.maybe_refresh_login()
        return self._user_id

    def needs_login_refresh(self) -> bool:
        return self.force_reauth or self.expiry is None or self.expiry < datetime.utcnow()

    async def maybe_refresh_login(self):
        if self.needs_login_refresh():
            try:
                await self._load_cached()
            except SessionExpired as e:
                log.debug(e)
                await self._login_via_google()
                self.force_reauth = False
            self.last_reauth = datetime.now()

    def _read_cache_file(self):
        with self.cache_path.open('rb') as f:
            try:
                expiry, userid, jwt_token, cookies = pickle.load(f)
            except (TypeError, ValueError) as e:
                raise SessionExpired(f'Found a cached session, but encountered an error loading it: {e}')
        return expiry, userid, jwt_token, cookies

    async def _load_cached(self):
        if self.force_reauth:
            raise SessionExpired('Forced reauth')
        elif self.cache_path.exists():
            expiry, userid, jwt_token, cookies = await get_running_loop().run_in_executor(None, self._read_cache_file)
            if expiry.tzinfo:
                expiry = expiry.astimezone(UTC).replace(tzinfo=None)
            if expiry < datetime.utcnow() or any(cookie.expires < time.time() for cookie in cookies):
                raise SessionExpired('Found a cached session, but it expired')

            await self._register_session(expiry, userid, jwt_token, cookies)
            log.debug(f'Loaded session for user={userid} with expiry={self._localize(expiry)}')
        else:
            raise SessionExpired('No cached session was found')

    async def _register_session(self, expiry: datetime, userid: str, jwt_token: str, cookies=None, save: bool = False):
        self.expiry = expiry
        self._user_id = userid
        session = await self._client.get_session()
        session.headers['Authorization'] = f'Basic {jwt_token}'
        if cookies is not None:
            for cookie in cookies:
                session.cookies.jar.set_cookie(cookie)

        if save:
            self._save_session(expiry, userid, jwt_token, list(session.cookies.jar))
            # loop = get_running_loop()
            # await loop.run_in_executor(None, self._save_session, expiry, userid, jwt_token, list(session.cookies.jar))

    def _save_session(self, expiry: datetime, userid: str, jwt_token: str, cookies):
        if not self.cache_path.parent.exists():
            self.cache_path.parent.mkdir(parents=True)
        log.debug(f'Saving session info in cache: {self.cache_path}')
        with self.cache_path.open('wb') as f:
            pickle.dump((expiry, userid, jwt_token, cookies), f)

    async def _get_oauth_token(self) -> str:
        headers = {
            'Sec-Fetch-Mode': 'cors',
            'X-Requested-With': 'XmlHttpRequest',
            'Referer': 'https://accounts.google.com/o/oauth2/iframe',
            'cookie': self.config.oauth_cookie,
        }
        params = {
            'action': ['issueToken'],
            'response_type': ['token id_token'],
            'login_hint': [self.config.oauth_login_hint],
            'client_id': [self.config.oauth_client_id],
            'origin': [NEST_URL],
            'scope': ['openid profile email https://www.googleapis.com/auth/nest-account'],
            'ss_domain': [NEST_URL],
        }
        session = await self._client.get_session()
        resp = (await session.get(OAUTH_URL, params=params, headers=headers)).json()
        resp_str = json.dumps(resp, indent=4, sort_keys=True)
        log.log(9, f'Received OAuth response: {resp_str}')
        try:
            return resp['access_token']
        except KeyError as e:
            raise RuntimeError(f'No access_token was found in the oauth response: {resp_str}') from e

    async def _login_via_google(self):
        token = await self._get_oauth_token()
        headers = {'Authorization': f'Bearer {token}', 'x-goog-api-key': NEST_API_KEY}
        params = {
            'embed_google_oauth_access_token': True,
            'expire_after': '3600s',
            'google_oauth_access_token': token,
            'policy_id': 'authproxy-oauth-policy',
        }
        session = await self._client.get_session()
        resp = (await session.post(JWT_URL, params=params, headers=headers)).json()
        log.log(9, f'Initialized session; response: {json.dumps(resp, indent=4, sort_keys=True)}')
        claims = resp['claims']
        expiry = _parse_datetime(claims['expirationTime'])
        await self._register_session(expiry, claims['subject']['nestId']['id'], resp['jwt'], save=True)
        log.debug(f'Initialized session for user={self._user_id!r} with expiry={self._localize(expiry)}')

    async def __aenter__(self):
        await self._lock.acquire()
        await self.maybe_refresh_login()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()

    def _localize(self, expiry: datetime) -> str:
        return expiry.replace(tzinfo=UTC).astimezone(self.config.time_zone).strftime('%Y-%m-%d %H:%M:%S %Z')


def _expand_with_children(types: Iterable[str]) -> set[str]:
    types = set(types)
    for p_type in tuple(types):
        if cls := NestObject._type_cls_map.get(p_type):
            types.update(cls.fetch_child_types)
    return types


def _type_not_found_description(obj_type: str, sub_type_key: str) -> str:
    if sub_type_key:
        try:
            return f'cls={NestObject._sub_type_cls_map[obj_type][sub_type_key].__name__}'
        except KeyError:
            return f'{obj_type=} with sub-type key={sub_type_key!r}'
    else:
        return f'{obj_type=}'


def _parse_datetime(dt_str: str) -> datetime:
    for dt_format in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ'):
        try:
            return datetime.strptime(dt_str, dt_format)
        except ValueError:
            pass
    raise ValueError(f'Could not parse {dt_str=} using any configured datetime format')
