"""
Library for interacting with the Nest thermostat via the web API

:author: Doug Skrypa
"""

import json
import logging
import pickle
import time
from contextlib import contextmanager
from datetime import datetime
from functools import cached_property
from threading import RLock, Event
from typing import ContextManager, Union, Optional, Mapping, Iterable, Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from requests import Session, RequestException

from requests_client.client import RequestsClient
from requests_client.user_agent import USER_AGENT_CHROME

from .utils import get_user_cache_dir
from .config import NestConfig
from .constants import JWT_URL, NEST_API_KEY, NEST_URL, OAUTH_URL,INIT_BUCKET_TYPES
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
        self._client = RequestsClient(NEST_URL, user_agent_fmt=USER_AGENT_CHROME, headers={'Referer': NEST_URL})
        self.auth = NestWebAuth(self.config, self._client, reauth)
        self._known_objects: dict[str, NestObj] = {}
        self.not_refreshing = Event()  # cleared while a refresh is pending
        self.not_refreshing.set()
        self._latest_transport_url = None
        self._latest_weather = None
        self._last_known_reauth = datetime.now()

    @property
    def user_id(self):
        return self.auth.user_id

    # region URLs

    @contextmanager
    def transport_url(self) -> ContextManager[RequestsClient]:
        with self.auth:
            log.debug('Using host:port={}:{}'.format(*self._transport_host_port))
            self._client.host, self._client.port = self._transport_host_port
            yield self._client

    @contextmanager
    def nest_url(self) -> ContextManager[RequestsClient]:
        with self.auth:
            log.debug('Using host:port={}:{}'.format(*self._nest_host_port))
            self._client.host, self._client.port = self._nest_host_port
            yield self._client

    @property
    def _transport_host_port(self) -> tuple[str, int | None]:
        if self._needs_transport_url_update():
            self.app_launch()
        return self._latest_transport_url.hostname, self._latest_transport_url.port

    def _needs_transport_url_update(self) -> bool:
        return self._latest_transport_url is None or self.auth.last_reauth > self._last_known_reauth

    # endregion

    # region Low Level Methods

    def app_launch(self, bucket_types: Iterable[str] = None, timeout: float = None) -> dict[str, Any]:
        with self.nest_url() as client:
            bucket_types = list(bucket_types) if bucket_types else []
            payload = {'known_bucket_types': bucket_types, 'known_bucket_versions': []}
            resp = client.post(f'api/0.1/user/{self.user_id}/app_launch', json=payload, timeout=timeout).json()
            if self._needs_transport_url_update():
                self._latest_weather = resp['weather_for_structures']
                self._latest_transport_url = urlparse(resp['service_urls']['urls']['transport_url'])
                self._last_known_reauth = self.auth.last_reauth
            return resp

    def get_buckets(self, types: Iterable[str], timeout: float = None) -> list[NestObjectDict]:
        return self.app_launch(types, timeout)['updated_buckets']

    def get_mobile_info(self):
        """Returns the same info as app_launch, but in a slightly different format"""
        with self.transport_url() as client:
            return client.get(f'v2/mobile/user.{self.user_id}').json()

    def get_weather_location(self) -> tuple[str, str]:
        if self._latest_weather is None:
            self.app_launch()
        location = next(iter(self._latest_weather.values()))['location']
        return location['zip'], location['country']

    def get_weather(self, zip_code: Union[str, int] = None, country_code: str = None) -> dict[str, Any]:
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
            zip_code, country_code = self.get_weather_location()

        country_code = country_code or 'US'
        with self.nest_url() as client:
            return client.get(f'api/0.1/weather/forecast/{zip_code},{country_code}').json()

    # endregion

    # region High Level Object Methods

    @cached_property
    def objects(self) -> dict[str, NestObj]:
        return self.get_objects(INIT_BUCKET_TYPES, False)

    @cached_property
    def parent_objects(self) -> dict[str, NestObj]:
        return {obj.serial: obj for obj in self.objects.values() if obj.parent_type is None}

    def get_objects(self, types: Iterable[str], cached: bool = True, children: bool = True) -> dict[str, NestObj]:
        types = set(types)
        if cached and (obj_dict := {k: v for k, v in self._known_objects.items() if v.type in types}):
            found_types = {obj.type for obj in obj_dict.values()}
            if missing := types.difference(found_types):
                log.debug(f'Found={found_types} requested={types} - retrieving {missing=}')
                obj_dict.update(self.get_objects(missing, False))
            return obj_dict

        orig_types = types
        if children:
            types = _expand_with_children(types)

        log.debug(f'Requesting buckets for {types=}')
        obj_dict = {obj['object_key']: NestObject.from_dict(obj, self) for obj in self.get_buckets(types)}
        log.debug('Found new objects: {}'.format(', '.join(sorted(obj_dict))))
        self._known_objects.update(obj_dict)
        if children and orig_types != types:
            obj_dict = {key: obj for key, obj in obj_dict.items() if obj.type in orig_types}
        return obj_dict

    def get_object(
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
        return self._get_object(self.get_objects([type], False, children), type, serial, _sub_type_key)

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

    # endregion

    # region Typed NestObject Getters

    def get_device(self, serial: str = None, cached: bool = True, children: bool = True) -> NestDevice:
        return self.get_object('device', serial, cached, children)

    def get_devices(self, cached: bool = True, children: bool = True) -> dict[str, NestDevice]:
        return self.get_objects(['device'], cached, children)

    def get_structure(self, serial: str = None, cached: bool = True) -> Structure:
        return self.get_object('structure', serial, cached)

    def get_structures(self, cached: bool = True) -> dict[str, Structure]:
        return self.get_objects(['structure'], cached)

    def get_user(self, serial: str = None, cached: bool = True) -> User:
        return self.get_object('user', serial, cached)

    def get_users(self, cached: bool = True) -> dict[str, User]:
        return self.get_objects(['user'], cached)

    def get_shared(self, serial: str = None, cached: bool = True) -> Shared:
        return self.get_object('shared', serial, cached)

    def get_shareds(self, cached: bool = True) -> dict[str, Shared]:
        return self.get_objects(['shared'], cached)

    @cached_property
    def devices(self) -> tuple[NestDevice]:
        return tuple(self.get_devices().values())

    @cached_property
    def structures(self) -> tuple[Structure]:
        return tuple(self.get_structures().values())

    @cached_property
    def users(self) -> tuple[User]:
        return tuple(self.get_users().values())

    @cached_property
    def shared(self) -> tuple[Shared]:
        return tuple(self.get_shareds().values())

    # endregion

    # region Refresh Methods

    def subscribe(self, objects: Iterable[NestObj], send_meta: bool = True, timeout: float = 5) -> list[NestObjectDict]:
        with self.transport_url() as client:
            payload = {'objects': [obj.subscribe_dict(send_meta) for obj in objects], 'timeout': 863}
            log.debug(f'Submitting subscribe request with {payload=}')
            resp = client.post('v5/subscribe', json=payload, timeout=timeout)
            return resp.json()['objects']

    def refresh_known_objects(self, subscribe: bool = True, send_meta: bool = True, timeout: float = None):
        self.refresh_objects(self._known_objects.values(), subscribe, send_meta, timeout=timeout)

    def refresh_objects(
        self,
        objects: Iterable[NestObj],
        subscribe: bool = True,
        send_meta: bool = True,
        *,
        timeout: float = None,
        children: bool = True,
    ):
        self.not_refreshing.clear()
        try:
            self._refresh_objects(objects, subscribe, send_meta, timeout=timeout, children=children)
        finally:
            self.not_refreshing.set()

    def _refresh_objects(
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
                    for obj in tuple(objects):
                        objects.update(obj.children.values())
                raw_objs = self.subscribe(objects, send_meta, timeout or 5)
            else:
                types = {obj.type for obj in objects}
                raw_objs = self.get_buckets(_expand_with_children(types) if children else types, timeout=timeout)
        except RequestException as e:
            log.debug(f'Refresh failed due to error: {e}')
        else:
            for raw_obj in raw_objs:
                key = raw_obj['object_key']
                if obj := self._known_objects.get(key):
                    obj._refresh(raw_obj)
                else:
                    self._known_objects[key] = NestObject.from_dict(raw_obj, self)

    # endregion


class NestWebAuth:
    def __init__(self, config: NestConfig, client: RequestsClient, force_reauth: bool = False):
        self._client = client
        self.config = config
        if 'oauth' not in self.config:
            raise ConfigError(self.config, 'Missing required oauth configs')
        self.cache_path = get_user_cache_dir('nest').joinpath('session.pickle')
        self.force_reauth = force_reauth
        self.last_reauth = None
        self.expiry = None
        self._lock = RLock()
        self._user_id = None

    @property
    def user_id(self) -> str:
        if self._user_id is None:
            self.maybe_refresh_login()
        return self._user_id

    @property
    def needs_login_refresh(self) -> bool:
        with self._lock:
            return self.force_reauth or self.expiry is None or self.expiry < datetime.utcnow()

    def maybe_refresh_login(self):
        with self._lock:
            if self.needs_login_refresh:
                try:
                    self._load_cached()
                except SessionExpired as e:
                    log.debug(e)
                    self._login_via_google()
                    self.force_reauth = False
                self.last_reauth = datetime.now()

    def _load_cached(self):
        if self.force_reauth:
            raise SessionExpired('Forced reauth')
        elif self.cache_path.exists():
            with self.cache_path.open('rb') as f:
                try:
                    expiry, userid, jwt_token, cookies = pickle.load(f)
                except (TypeError, ValueError) as e:
                    raise SessionExpired(f'Found a cached session, but encountered an error loading it: {e}')

            if expiry.tzinfo:
                expiry = expiry.astimezone(UTC).replace(tzinfo=None)
            if expiry < datetime.utcnow() or any(cookie.expires < time.time() for cookie in cookies):
                raise SessionExpired('Found a cached session, but it expired')

            self._register_session(expiry, userid, jwt_token, cookies)
            log.debug(f'Loaded session for user={userid} with expiry={self._localize(expiry)}')
        else:
            raise SessionExpired('No cached session was found')

    @property
    def _session(self) -> 'Session':
        return self._client.session

    def _register_session(self, expiry: datetime, userid: str, jwt_token: str, cookies=None, save: bool = False):
        self.expiry = expiry
        self._user_id = userid
        self._session.headers['Authorization'] = f'Basic {jwt_token}'
        if cookies is not None:
            for cookie in cookies:
                self._session.cookies.set_cookie(cookie)

        if save:
            if not self.cache_path.parent.exists():
                self.cache_path.parent.mkdir(parents=True)
            log.debug(f'Saving session info in cache: {self.cache_path}')
            with self.cache_path.open('wb') as f:
                pickle.dump((expiry, userid, jwt_token, list(self._session.cookies)), f)

    def _get_oauth_token(self) -> str:
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
        resp = self._session.get(OAUTH_URL, params=params, headers=headers).json()
        log.log(9, 'Received OAuth response: {}'.format(json.dumps(resp, indent=4, sort_keys=True)))
        return resp['access_token']

    def _login_via_google(self):
        token = self._get_oauth_token()
        headers = {'Authorization': f'Bearer {token}', 'x-goog-api-key': NEST_API_KEY}
        params = {
            'embed_google_oauth_access_token': True,
            'expire_after': '3600s',
            'google_oauth_access_token': token,
            'policy_id': 'authproxy-oauth-policy',
        }
        resp = self._session.post(JWT_URL, params=params, headers=headers).json()
        log.log(9, 'Initialized session; response: {}'.format(json.dumps(resp, indent=4, sort_keys=True)))
        claims = resp['claims']
        expiry = datetime.strptime(claims['expirationTime'], '%Y-%m-%dT%H:%M:%S.%fZ')
        self._register_session(expiry, claims['subject']['nestId']['id'], resp['jwt'], save=True)
        log.debug(f'Initialized session for user={self._user_id!r} with expiry={self._localize(expiry)}')

    def __enter__(self):
        self._lock.acquire()
        self.maybe_refresh_login()

    def __exit__(self, exc_type, exc_val, exc_tb):
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
