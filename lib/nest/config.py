"""
Nest config

:author: Doug Skrypa
"""

import logging
from configparser import ConfigParser, NoSectionError, NoOptionError
from functools import cached_property
from pathlib import Path
from typing import Optional, Mapping

__all__ = ['NestConfig', 'DEFAULT_CONFIG_PATH', 'CONFIG_ITEMS']
log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = '~/.config/nest.cfg'
CONFIG_ITEMS = {
    'credentials': {'email': 'email address'},
    'device': {'serial': 'thermostat serial number'},
    'oauth': {'cookie': 'OAuth Cookie', 'login_hint': 'OAuth login_hint', 'client_id': 'OAuth client_id'},
    'units': {'temperature': 'temperature unit'},
}


class NestConfig:
    def __init__(self, path: str = None, overrides: Mapping[str, Optional[str]] = None):
        self.path = Path(path or DEFAULT_CONFIG_PATH).expanduser()
        self._overrides = overrides or {}

    @cached_property
    def _data(self) -> ConfigParser:
        config = ConfigParser()
        if self.path.exists():
            with self.path.open('r', encoding='utf-8') as f:
                config.read_file(f)
        return config

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, section: str, key: str, name: str = None, new_value=None, save: bool = False, required: bool = False):
        name = name or key
        try:
            cfg_value = self._data.get(section, key)
        except (NoSectionError, NoOptionError):
            cfg_value = None
        if required and not cfg_value and not new_value:
            try:
                new_value = input(f'Please enter your Nest {name}: ').strip()
            except EOFError as e:
                raise RuntimeError('Unable to read stdin (this is often caused by piped input)') from e
            if not new_value:
                raise ValueError(f'Invalid {name}')
            self.set(section, key, new_value)
        elif new_value and save:
            self.set(section, key, new_value)
        return new_value or cfg_value

    def set(self, section, key, value):
        try:
            self._data.set(section, key, value)
        except NoSectionError:
            self._data.add_section(section)
            self._data.set(section, key, value)
        with self.path.open('w', encoding='utf-8') as f:
            self._data.write(f)

    def delete(self, section, key):
        try:
            self._data.remove_option(section, key)
        except (NoSectionError, NoOptionError):
            pass
        else:
            with self.path.open('w', encoding='utf-8') as f:
                self._data.write(f)

    @cached_property
    def email(self) -> str:
        return self.get('credentials', 'email', 'email address', self._overrides.get('email'), required=True)

    @property
    def serial(self) -> Optional[str]:
        return self.get('device', 'serial', 'thermostat serial number', self._overrides.get('serial'))

    @property
    def oauth_cookie(self):
        return self.get('oauth', 'cookie', 'OAuth Cookie', required=True)

    @property
    def oauth_login_hint(self):
        return self.get('oauth', 'login_hint', 'OAuth login_hint', required=True)

    @property
    def oauth_client_id(self):
        return self.get('oauth', 'client_id', 'OAuth client_id', required=True)

    @cached_property
    def temp_unit(self) -> str:
        value = self.get(
            'units', 'temperature', 'temperature unit', self._overrides.get('temp_unit'), save=True, required=True
        )
        if value:
            lc_value = value.lower()
            if lc_value != value and lc_value in {'f', 'c'}:
                self.set('units', 'temperature', lc_value)
                value = lc_value
            elif lc_value in {'fahrenheit', 'celsius'}:
                value = lc_value[0]
                self.set('units', 'temperature', value)

        if value not in {'f', 'c'}:
            self.delete('units', 'temperature')
            raise ValueError(f'Invalid temperature unit={value!r} - must be \'c\' or \'f\'')
        return value

    def maybe_set(self, section: str, key: str, value):
        try:
            key_name_map = CONFIG_ITEMS[section]
        except KeyError as e:
            sections = ', '.join(sorted(CONFIG_ITEMS))
            raise ValueError(f'Invalid {section=} - choose one of: {sections}') from e
        try:
            name = key_name_map[key]
        except KeyError as e:
            keys = ', '.join(sorted(key_name_map))
            raise ValueError(f'Invalid [{section}] {key=} - choose one of: {keys}') from e
        try:
            old = self._data.get(section, key)
        except (NoSectionError, NoOptionError):
            old = None
        else:
            self.delete(section, key)

        try:
            if name == 'temperature unit':
                try:
                    del self.__dict__['temp_unit']
                except KeyError:
                    pass
                self._overrides['temp_unit'] = value
                new_val = self.temp_unit
            else:
                new_val = self.get(section, key, name, value, save=True)
        except Exception:
            if old is not None:
                log.debug(f'Restoring old {section=} {key=} value={old!r}')
                self.set(section, key, old)
            raise
