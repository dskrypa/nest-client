"""
Exceptions for working with Nest thermostats

:author: Doug Skrypa
"""

from __future__ import annotations

from os import environ
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import NestConfig

__all__ = [
    'NestException',
    'SessionExpired',
    'AuthorizationError',
    'ConfigError',
    'TimeNotFound',
    'NestObjectNotFound',
    'TableFormatException',
    'DictAttrFieldNotFoundError',
]


class NestException(Exception):
    """Base exception"""


class SessionExpired(NestException):
    pass


class AuthorizationError(NestException):
    pass


class ConfigError(NestException):
    def __init__(self, config: NestConfig, message: str):
        self.config = config
        self.message = message

    def __str__(self) -> str:
        return f'{self.__class__.__name__}: {self.message} in {self.config.path.as_posix()}\nenv: {environ}'


class TimeNotFound(NestException):
    pass


class NestObjectNotFound(NestException):
    pass


class TableFormatException(NestException):
    def __init__(self, scope, fmt_str, value, exc, *args):
        self.scope = scope
        self.fmt_str = fmt_str
        self.value = value
        self.exc = exc
        super().__init__(*args)

    def __str__(self) -> str:
        return (
            f'Error formatting {self.scope}: {type(self.exc).__name__} {self.exc}'
            f'\nFormat string: {self.fmt_str!r}\nContent: {self.value}'
        )


class DictAttrFieldNotFoundError(NestException):
    def __init__(self, obj, prop_name, attr, path_repr):
        self.obj = obj
        self.prop_name = prop_name
        self.attr = attr
        self.path_repr = path_repr

    def __str__(self) -> str:
        return (
            f'{type(self.obj).__name__!r} object has no attribute {self.prop_name!r}'
            f' ({self.path_repr} not found in {self.obj!r}.{self.attr})'
        )
