"""
Library for interacting with the Nest thermostat via the cloud API

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
from typing import TYPE_CHECKING, ContextManager, Union, Sequence, Optional, Mapping
from urllib.parse import urlparse

from requests_client import RequestsClient, USER_AGENT_CHROME
from tz_aware_dt.tz_aware_dt import datetime_with_tz, localize, TZ_LOCAL, TZ_UTC

from .utils import get_user_cache_dir
from .config import NestConfig
from .constants import JWT_URL, NEST_API_KEY, NEST_URL, OAUTH_URL,INIT_BUCKET_TYPES
from .exceptions import SessionExpired, ConfigError, NestObjectNotFound
from .entities import NestObject, NestObj

if TYPE_CHECKING:
    from requests import Response

__all__ = ['NestWebSession']
log = logging.getLogger(__name__)


class NestWebSession:
    _nest_host_port = ('home.nest.com', None)

    def __init__(self, config_path: str = None, reauth: bool = False, overrides: Mapping[str, Optional[str]] = None):
        self.config = NestConfig(config_path, overrides)
        self.cache_path = get_user_cache_dir('nest').joinpath('session.pickle')
        self.client = RequestsClient(NEST_URL, user_agent_fmt=USER_AGENT_CHROME, headers={'Referer': NEST_URL})
        self._lock = RLock()
        self.expiry = None
        self._user_id = None
        self._reauth = reauth

    @property
    def user_id(self):
        if self._user_id is None:
            self._maybe_refresh_login()
        return self._user_id

    @user_id.setter
    def user_id(self, value):
        self._user_id = value

    @contextmanager
    def transport_url(self) -> ContextManager[RequestsClient]:
        with self._lock:
            self._maybe_refresh_login()
            log.debug('Using host:port={}:{}'.format(*self._transport_host_port))
            self.client.host, self.client.port = self._transport_host_port
            yield self.client

    @contextmanager
    def nest_url(self) -> ContextManager[RequestsClient]:
        with self._lock:
            self._maybe_refresh_login()
            log.debug('Using host:port={}:{}'.format(*self._nest_host_port))
            self.client.host, self.client.port = self._nest_host_port
            yield self.client

    @cached_property
    def service_urls(self):
        return self.app_launch().json()['service_urls']

    @cached_property
    def _transport_host_port(self):
        transport_url = urlparse(self.service_urls['urls']['transport_url'])
        return transport_url.hostname, transport_url.port

    @property
    def needs_login_refresh(self) -> bool:
        with self._lock:
            return self.expiry is None or self.expiry < datetime.now(TZ_LOCAL)

    def _maybe_refresh_login(self):
        with self._lock:
            if self.needs_login_refresh:
                for key in ('service_urls', '_transport_host_port'):
                    try:
                        del self.__dict__[key]
                    except KeyError:
                        pass

                if 'oauth' in self.config:
                    try:
                        self._load_cached()
                    except SessionExpired as e:
                        log.debug(e)
                        self._login_via_google()
                else:
                    raise ConfigError('Missing required oauth configs')

    def _load_cached(self):
        if self._reauth:
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

    def _register_session(self, expiry: datetime, userid: str, jwt_token: str, cookies=None, save: bool = False):
        self.expiry = expiry
        self.user_id = userid
        self.client.session.headers['Authorization'] = f'Basic {jwt_token}'
        if cookies is not None:
            for cookie in cookies:
                self.client.session.cookies.set_cookie(cookie)

        if save:
            if not self.cache_path.parent.exists():
                self.cache_path.parent.mkdir(parents=True)
            log.debug(f'Saving session info in cache: {self.cache_path}')
            with self.cache_path.open('wb') as f:
                pickle.dump((expiry, userid, jwt_token, list(self.client.session.cookies)), f)

    def _get_oauth_token(self) -> str:
        headers = {
            'Sec-Fetch-Mode': 'cors',
            'X-Requested-With': 'XmlHttpRequest',
            'Referer': 'https://accounts.google.com/o/oauth2/iframe',
            'cookie': self.config.oauth_cookie,
        }
        # token_url = self.config.get('oauth', 'token_url', 'OAuth Token URL', required=True)
        # resp = self.client.session.get(token_url, headers=headers)
        params = {
            'action': ['issueToken'],
            'response_type': ['token id_token'],
            'login_hint': [self.config.oauth_login_hint],
            'client_id': [self.config.oauth_client_id],
            'origin': [NEST_URL],
            'scope': ['openid profile email https://www.googleapis.com/auth/nest-account'],
            'ss_domain': [NEST_URL],
        }
        resp = self.client.session.get(OAUTH_URL, params=params, headers=headers).json()
        log.log(9, 'Received OAuth response: {}'.format(json.dumps(resp, indent=4, sort_keys=True)))
        return resp['access_token']

    def _login_via_google(self):
        token = self._get_oauth_token()
        headers = {'Authorization': f'Bearer {token}', 'x-goog-api-key': NEST_API_KEY}
        params = {
            'embed_google_oauth_access_token': True, 'expire_after': '3600s',
            'google_oauth_access_token': token, 'policy_id': 'authproxy-oauth-policy',
        }
        resp = self.client.session.post(JWT_URL, params=params, headers=headers).json()
        log.log(9, 'Initialized session; response: {}'.format(json.dumps(resp, indent=4, sort_keys=True)))
        claims = resp['claims']
        expiry = datetime_with_tz(claims['expirationTime'], '%Y-%m-%dT%H:%M:%S.%fZ', TZ_UTC).astimezone(TZ_LOCAL)
        self._register_session(expiry, claims['subject']['nestId']['id'], resp['jwt'], save=True)
        log.debug(f'Initialized session for user={self.user_id!r} with expiry={localize(expiry)}')

    def app_launch(self, bucket_types: Sequence[str] = None) -> 'Response':
        with self.nest_url() as client:
            payload = {'known_bucket_types': bucket_types or [], 'known_bucket_versions': []}
            return client.post(f'api/0.1/user/{self.user_id}/app_launch', json=payload)

    def get_object(self, type: str, serial: str = None, cached: bool = False) -> NestObj:  # noqa
        if cached and (obj_dict := self.__dict__.get('objects')):
            try:
                return self._get_object(obj_dict, type, serial)
            except NestObjectNotFound:  # let ValueError propagate
                pass  # try fresh objects

        return self._get_object(self.get_objects([type]), type, serial)

    def _get_object(self, obj_dict: dict[str, NestObj], type: str, serial: str = None) -> NestObj:  # noqa
        serial = serial or self.config.serial
        if serial:
            object_key = f'{type}.{serial}'
            try:
                return obj_dict[object_key]
            except KeyError:
                raise NestObjectNotFound(f'Could not find {object_key=} (found={list(obj_dict)})')
        else:
            if len(obj_dict) == 1:
                return next(iter(obj_dict.values()))
            elif not obj_dict:
                raise NestObjectNotFound(f'No {type=} objects were found from {self}')
            else:
                raise ValueError(
                    f'A serial number is required - found {len(obj_dict)} {type=} objects: {list(obj_dict)}'
                )

    def get_objects(self, types: Sequence[str], cached: bool = False) -> dict[str, NestObj]:
        if cached and (obj_dict := self.__dict__.get('objects')):
            return obj_dict
        objects = self.app_launch(types).json()['updated_buckets']
        return {obj['object_key']: NestObject.from_dict(obj, self) for obj in objects}

    @cached_property
    def objects(self) -> dict[str, NestObj]:
        return self.get_objects(INIT_BUCKET_TYPES)

    @cached_property
    def parent_objects(self) -> dict[str, NestObj]:
        return {obj.serial: obj for obj in self.objects.values() if obj.parent_type is None}

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
