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
from threading import RLock
from typing import TYPE_CHECKING, ContextManager, Union, Optional, Mapping, Iterable
from urllib.parse import urlparse

from requests_client import RequestsClient, USER_AGENT_CHROME
from tz_aware_dt.tz_aware_dt import datetime_with_tz, localize, TZ_LOCAL, TZ_UTC

from .utils import get_user_cache_dir
from .config import NestConfig
from .constants import JWT_URL, NEST_API_KEY, NEST_URL, OAUTH_URL,INIT_BUCKET_TYPES
from .exceptions import SessionExpired, ConfigError, NestObjectNotFound
from .entities import NestObjectDict, NestObject, NestObj, NestDevice, Structure, User

if TYPE_CHECKING:
    from requests import Response, Session

__all__ = ['NestWebClient']
log = logging.getLogger(__name__)


class NestWebClient:
    _nest_host_port = ('home.nest.com', None)

    def __init__(self, config_path: str = None, reauth: bool = False, overrides: Mapping[str, Optional[str]] = None):
        self._user_id = None
        self.config = NestConfig(config_path, overrides)
        self._client = RequestsClient(NEST_URL, user_agent_fmt=USER_AGENT_CHROME, headers={'Referer': NEST_URL})
        self.auth = NestWebAuth(self, reauth)
        self._known_objects: dict[str, NestObj] = {}

    @property
    def user_id(self):
        if self._user_id is None:
            self.auth.maybe_refresh_login()
        return self._user_id

    @user_id.setter
    def user_id(self, value):
        self._user_id = value

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

    @cached_property
    def service_urls(self):
        return self.app_launch().json()['service_urls']

    @cached_property
    def _transport_host_port(self):
        transport_url = urlparse(self.service_urls['urls']['transport_url'])
        return transport_url.hostname, transport_url.port

    # endregion

    # region Low Level Methods

    def get_mobile_info(self):
        """Returns the same info as app_launch, but in a slightly different format"""
        with self.transport_url() as client:
            return client.get(f'v2/mobile/user.{self.user_id}').json()

    def get_weather(self, zip_code: Union[str, int] = None, country_code: str = 'US'):
        """
        Get the weather forecast.  Response format::
            {
              "display_city":"...", "city":"...",
              "forecast":{
                "hourly":[{"time":1569769200, "temp":74.0, "humidity":55},...],
                "daily":[{
                  "conditions":"Partly Cloudy", "date":1569729600, "high_temperature":77.0, "icon":"partlycloudy",
                  "low_temperature":60.0
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
        :return dict: The parsed response
        """
        if zip_code is None:
            resp = self.app_launch().json()
            location = next(iter(resp['weather_for_structures'].values()))['location']
            zip_code = location['zip']
            country_code = country_code or location['country']

        with self.nest_url() as client:
            return client.get(f'api/0.1/weather/forecast/{zip_code},{country_code}').json()

    def app_launch(self, bucket_types: Iterable[str] = None) -> 'Response':
        with self.nest_url() as client:
            payload = {'known_bucket_types': list(bucket_types) or [], 'known_bucket_versions': []}
            return client.post(f'api/0.1/user/{self.user_id}/app_launch', json=payload)

    def get_buckets(self, types: Iterable[str]) -> list[NestObjectDict]:
        return self.app_launch(types).json()['updated_buckets']

    # endregion

    # region High Level Object Methods

    @cached_property
    def objects(self) -> dict[str, NestObj]:
        return self.get_objects(INIT_BUCKET_TYPES, False)

    @cached_property
    def parent_objects(self) -> dict[str, NestObj]:
        return {obj.serial: obj for obj in self.objects.values() if obj.parent_type is None}

    def get_objects(self, types: Iterable[str], cached: bool = True) -> dict[str, NestObj]:
        if cached and (obj_dict := self.__dict__.get('objects')):
            types = set(types)
            if obj_dict := {k: v for k, v in obj_dict.items() if v.type in types}:
                if missing := types.difference({obj.type for obj in obj_dict.values()}):
                    obj_dict.update(self.get_objects(missing, False))
                return obj_dict

        obj_dict = {obj['object_key']: NestObject.from_dict(obj, self) for obj in self.get_buckets(types)}
        self._known_objects.update(obj_dict)
        return obj_dict

    def get_object(self, type: str, serial: str = None, cached: bool = True) -> NestObj:  # noqa
        if cached and (obj_dict := self.__dict__.get('objects')):
            try:
                return self._get_object(obj_dict, type, serial)
            except NestObjectNotFound:  # let ValueError propagate
                pass  # try fresh objects

        obj = self._get_object(self.get_objects([type]), type, serial)
        self._known_objects[obj.key] = obj
        return obj

    def _get_object(self, obj_map: dict[str, NestObj], type: str, serial: str = None) -> NestObj:  # noqa
        serial = serial or self.config.serial
        if serial:
            object_key = f'{type}.{serial}'
            try:
                return obj_map[object_key]
            except KeyError:
                raise NestObjectNotFound(f'Could not find {object_key=} (found={list(obj_map)})')
        else:
            if len(obj_map) == 1:
                return next(iter(obj_map.values()))
            elif not obj_map:
                raise NestObjectNotFound(f'No {type=} objects were found from {self}')
            else:
                raise ValueError(f'A serial number is required - found {len(obj_map)} {type=} objects: {list(obj_map)}')

    def refresh_known_objects(self):
        types = {obj.type for obj in self._known_objects.values()}
        for raw_obj in self.get_buckets(types):
            key = raw_obj['object_key']
            if obj := self._known_objects.get(key):
                obj._refresh(raw_obj)
            else:
                self._known_objects[key] = NestObject.from_dict(raw_obj, self)

    # endregion

    # region Typed NestObject Getters

    def get_device(self, serial: str = None, cached: bool = True) -> NestDevice:
        return self.get_object('device', serial, cached)

    def get_devices(self, cached: bool = True) -> dict[str, NestDevice]:
        return self.get_objects(['device'], cached)

    def get_structure(self, serial: str = None, cached: bool = True) -> Structure:
        return self.get_object('structure', serial, cached)

    def get_structures(self, cached: bool = True) -> dict[str, Structure]:
        return self.get_objects(['structure'], cached)

    def get_user(self, serial: str = None, cached: bool = True) -> User:
        return self.get_object('user', serial, cached)

    def get_users(self, cached: bool = True) -> dict[str, User]:
        return self.get_objects(['user'], cached)

    # endregion


class NestWebAuth:
    def __init__(self, client: 'NestWebClient', force_reauth: bool = False):
        self.client = client
        self.config = client.config
        if 'oauth' not in self.config:
            raise ConfigError('Missing required oauth configs')
        self.cache_path = get_user_cache_dir('nest').joinpath('session.pickle')
        self.force_reauth = force_reauth
        self.expiry = None
        self._lock = RLock()

    @property
    def needs_login_refresh(self) -> bool:
        with self._lock:
            return self.force_reauth or self.expiry is None or self.expiry < datetime.now(TZ_LOCAL)

    def maybe_refresh_login(self):
        with self._lock:
            if self.needs_login_refresh:
                self._reset_urls()
                try:
                    self._load_cached()
                except SessionExpired as e:
                    log.debug(e)
                    self._login_via_google()
                    self.force_reauth = False

    def _reset_urls(self):
        for key in ('service_urls', '_transport_host_port'):
            try:
                del self.client.__dict__[key]
            except KeyError:
                pass

    def _load_cached(self):
        if self.force_reauth:
            raise SessionExpired('Forced reauth')
        elif self.cache_path.exists():
            with self.cache_path.open('rb') as f:
                try:
                    expiry, userid, jwt_token, cookies = pickle.load(f)
                except (TypeError, ValueError) as e:
                    raise SessionExpired(f'Found a cached session, but encountered an error loading it: {e}')

            if expiry < datetime.now(TZ_LOCAL) or any(cookie.expires < time.time() for cookie in cookies):
                raise SessionExpired('Found a cached session, but it expired')

            self._register_session(expiry, userid, jwt_token, cookies)
            log.debug(f'Loaded session for user={userid} with expiry={localize(expiry)}')
        else:
            raise SessionExpired('No cached session was found')

    @property
    def _session(self) -> 'Session':
        return self.client._client.session

    def _register_session(self, expiry: datetime, userid: str, jwt_token: str, cookies=None, save: bool = False):
        self.expiry = expiry
        self.client.user_id = userid
        self.client._client.session.headers['Authorization'] = f'Basic {jwt_token}'
        if cookies is not None:
            for cookie in cookies:
                self.client._client.session.cookies.set_cookie(cookie)

        if save:
            if not self.cache_path.parent.exists():
                self.cache_path.parent.mkdir(parents=True)
            log.debug(f'Saving session info in cache: {self.cache_path}')
            with self.cache_path.open('wb') as f:
                pickle.dump((expiry, userid, jwt_token, list(self.client._client.session.cookies)), f)

    def _get_oauth_token(self) -> str:
        headers = {
            'Sec-Fetch-Mode': 'cors',
            'X-Requested-With': 'XmlHttpRequest',
            'Referer': 'https://accounts.google.com/o/oauth2/iframe',
            'cookie': self.config.oauth_cookie,
        }
        # token_url = self.config.get('oauth', 'token_url', 'OAuth Token URL', required=True)
        # resp = self._session.get(token_url, headers=headers)
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
        expiry = datetime_with_tz(claims['expirationTime'], '%Y-%m-%dT%H:%M:%S.%fZ', TZ_UTC).astimezone(TZ_LOCAL)
        self._register_session(expiry, claims['subject']['nestId']['id'], resp['jwt'], save=True)
        log.debug(f'Initialized session for user={self.client.user_id!r} with expiry={localize(expiry)}')

    def __enter__(self):
        self._lock.acquire()
        self.maybe_refresh_login()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()
