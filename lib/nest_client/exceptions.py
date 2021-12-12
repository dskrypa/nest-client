"""
Exceptions for working with Nest thermostats

:author: Doug Skrypa
"""


class NestException(Exception):
    """Base exception"""


class SessionExpired(NestException):
    pass


class AuthorizationError(NestException):
    pass


class ConfigError(NestException):
    pass


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

    def __str__(self):
        msg_fmt = 'Error formatting {}: {} {}\nFormat string: {!r}\nContent: {}'
        return msg_fmt.format(self.scope, type(self.exc).__name__, self.exc, self.fmt_str, self.value)


class DictAttrFieldNotFoundError(NestException):
    def __init__(self, obj, prop_name, attr, path_repr):
        self.obj = obj
        self.prop_name = prop_name
        self.attr = attr
        self.path_repr = path_repr

    def __str__(self):
        fmt = '{!r} object has no attribute {!r} ({} not found in {!r}.{})'
        return fmt.format(type(self.obj).__name__, self.prop_name, self.path_repr, self.obj, self.attr)
