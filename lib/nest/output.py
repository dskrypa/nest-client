"""
Output formatting utilities.

:author: Doug Skrypa
"""

import json
import logging
import pprint
import re
import sys
from collections import UserDict
from collections.abc import KeysView, ValuesView
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from difflib import unified_diff
from functools import cached_property
from io import StringIO
from shutil import get_terminal_size
from traceback import format_tb
from types import GeneratorType, TracebackType
from typing import Union, Collection, TextIO, Optional, Mapping, Any, Type, Iterable, Sized, Container
from unicodedata import normalize

import yaml
from colored import stylize, fg as _fg, bg as _bg
try:
    from wcwidth import wcswidth
except ImportError:
    wcswidth = len

from .exceptions import TableFormatException
from .utils import ClearableCachedPropertyMixin, replacement_itemgetter

__all__ = ['Column', 'SimpleColumn', 'Table', 'TableBar', 'HeaderRow', 'colored', 'Printer', 'cdiff']
log = logging.getLogger(__name__)

ANSI_COLOR_RX = re.compile(r'(\033\[\d+;?\d*;?\d*m)(.*)(\033\[\d+;?\d*;?\d*m)')
Row = Union[Mapping[str, Any], 'TableBar', 'HeaderRow', Type['TableBar'], Type['HeaderRow']]


def colored(text, fg=None, do_color: bool = True, bg=None):
    if fg is not None and bg is not None:
        colors = (_fg(fg), _bg(bg))
    else:
        colors = _fg(fg) if fg is not None else _bg(bg) if bg is not None else ()
    return stylize(text, colors) if do_color and colors else text


def cdiff(path1, path2, n: int = 3):
    with open(path1, 'r', encoding='utf-8') as f1, open(path2, 'r', encoding='utf-8') as f2:
        _cdiff(f1.read().splitlines(), f2.read().splitlines(), path1, path2, n=n)


def _cdiff(a, b, name_a: str = '', name_b: str = '', n: int = 3):
    for i, line in enumerate(unified_diff(a, b, name_a, name_b, n=n, lineterm='')):
        if line.startswith('+') and i > 1:
            print(colored(line, 2))
        elif line.startswith('-') and i > 1:
            print(colored(line, 1))
        elif line.startswith('@@ '):
            print(colored(line, 6), end='\n\n')
        else:
            print(line)


# region Table Formatting


class Column:
    """
    An output column metadata handler

    Column width can be specified literally or determined dynamically...
    - If width is a number, then that value is used
    - If width is a collection, then the maximum length of the relevant elements that it contains is used
    - Relevant element discovery logic:
        - Treat width as a dict with .values() being dicts that contain an element with the given key
        - Treat width as a sequence with values being dicts that contain an element with the given key
        - Treat width as a sequence where all values are relevant
    - If the length of the title is greater than the current width, take that length instead

    :param str key: Row key associated with this column
    :param str title: Column header
    :param width: Width of this column (can auto-detect if passed values for this column)
    :param bool display: Include this column in output (default: True)
    :param str align: String formatting alignment indicator (default: left; example: '>' for right)
    :param str ftype: String formatting type/format indicator (default: none; example: ',d' for thousands indicator)
    """

    def __init__(self, key, title, width, display=True, align='', ftype='', formatter=None):
        self.key = key
        self.title = str(title)
        self._width = 0
        self.display = display
        self.align = align
        self.ftype = ftype
        self.formatter = formatter
        self.width = width

    def __repr__(self):
        return '<{}({!r}, {!r})>'.format(type(self).__name__, self.key, self.title)

    @property
    def _test_fmt(self):
        return '{{:{}{}}}'.format(self.align, self.ftype)

    @property
    def _row_fmt(self):
        return '{{:{}{}{}}}'.format(self.align, self.width, self.ftype)

    @property
    def row_fmt(self):
        return '{{0[{}]:{}{}{}}}'.format(self.key, self.align, self.width, self.ftype)

    @property
    def _header_fmt(self):
        return '{{:{}{}}}'.format(self.align, self.width)

    @property
    def header_fmt(self):
        return '{{0[{}]:{}{}}}'.format(self.key, self.align, self.width)

    @contextmanager
    def _temp_width(self, value):
        orig_width = self._width
        try:
            test_val = self._test_fmt.format(value)
        except ValueError:
            test_val = str(value)

        char_count = len(test_val)
        str_width = _mono_width(test_val)
        if char_count != str_width and str_width > 0:
            diff = str_width - char_count
            self._width -= diff

        try:
            yield
        finally:
            self._width = orig_width

    def _format(self, value):
        try:
            return self._row_fmt.format(value)
        except ValueError:
            return self._header_fmt.format(value)

    def format(self, value):
        with self._temp_width(value):
            try:
                if self.formatter:
                    return self.formatter(value, self._format(value))
                else:
                    if isinstance(value, str) and (m := ANSI_COLOR_RX.match(value)):
                        prefix, value, suffix = m.groups()
                        return prefix + self._format(value) + suffix
                    else:
                        return self._format(value)
            except TypeError as e:
                raise TableFormatException('column', self.row_fmt, value, e) from e

    @property
    def width(self):
        return self._width

    @width.setter
    def width(self, value):
        try:
            self._width = max(self._calc_width(value), _mono_width(self.title))
        except (ValueError, TypeError) as e:
            try:
                raise ValueError('{}: Unable to determine width (likely no values were found)'.format(self)) from e
            except ValueError as e2:
                raise ValueError('No results.') from e2

    def _len(self, text):
        char_count = len(text)
        str_width = _mono_width(text)
        if (char_count != str_width) and not self.formatter:
            self.formatter = lambda a, b: b  # Force Table.format_row to delegate formatting to Column.format
        return str_width

    def _calc_width(self, width):
        fmt = self._test_fmt
        try:
            return int(width)
        except TypeError:
            try:
                return max(self._len(fmt.format(e[self.key])) for e in width.values())
            except (KeyError, TypeError, AttributeError):
                try:
                    return max(self._len(fmt.format(e[self.key])) for e in width)
                except (KeyError, TypeError, AttributeError):
                    try:
                        return max(self._len(fmt.format(obj)) for obj in width)
                    except ValueError as e:
                        if 'Unknown format code' in str(e):
                            values = []
                            for obj in width:
                                try:
                                    values.append(fmt.format(obj))
                                except ValueError:
                                    values.append(str(obj))
                            return max(self._len(val) for val in values)


class SimpleColumn(Column):
    """
    An output column metadata handler

    :param str title: Column header & row key associated with this column
    :param width: Width of this column (can auto-detect if passed values for this column)
    :param bool display: Include this column in output (default: True)
    :param str align: String formatting alignment indicator (default: left; example: '>' for right)
    :param str ftype: String formatting type/format indicator (default: none; example: ',d' for thousands indicator)
    """

    def __init__(self, title, width=0, display=True, align='', ftype='', formatter=None):
        super().__init__(title, title, width, display, align, ftype, formatter)


class TableBar:
    char = '-'

    def __init__(self, char: str = '-'):
        self.char = char

    def __getitem__(self, item):
        return None


class HeaderRow:
    bar = False

    def __init__(self, bar: bool = False):
        self.bar = bar

    def __getitem__(self, item):
        return None


class Table(ClearableCachedPropertyMixin):
    def __init__(
        self,
        *columns: Union[Column, SimpleColumn],
        auto_header: bool = True,
        auto_bar: bool = True,
        sort: bool = False,
        sort_by: Union[Collection, str, None] = None,
        update_width: bool = False,
        fix_ansi_width: bool = False,
        file: Optional[TextIO] = None,
    ):
        self._columns = list(columns[0] if len(columns) == 1 and isinstance(columns[0], GeneratorType) else columns)
        self.auto_header = auto_header
        self.auto_bar = auto_bar
        self.sort = sort
        self.sort_by = sort_by
        self.update_width = update_width
        self.fix_ansi_width = fix_ansi_width
        self._flush = file is None
        if file is not None:
            self._file = file
            self._stdout = False
        else:
            self._stdout = True
            if sys.stdout.encoding.lower().startswith('utf'):
                self._file = sys.stdout
            else:
                self._file = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

    def __getitem__(self, item):
        for c in self.columns:
            if c.key == item:
                return c
        raise KeyError(item)

    def append(self, column: Union[Column, SimpleColumn]):
        self._columns.append(column)
        if column.display:
            self.clear_cached_properties()

    def toggle_display(self, key: str, display: bool = None):
        column = self[key]
        column.display = (not column.display) if display is None else display
        self.clear_cached_properties()

    @cached_property
    def columns(self) -> list[Union[Column, SimpleColumn]]:
        return [c for c in self._columns if c.display]

    @cached_property
    def keys(self) -> list[str]:
        return [c.key for c in self.columns]

    @cached_property
    def header_fmt(self) -> str:
        return '  '.join(c.header_fmt for c in self.columns)

    @cached_property
    def headers(self) -> dict[str, str]:
        return {c.key: c.title for c in self.columns if c.display}

    @cached_property
    def row_fmt(self) -> str:
        return '  '.join(c.row_fmt for c in self.columns)

    @cached_property
    def has_custom_formatter(self) -> bool:
        return any(c.formatter is not None for c in self.columns)

    @cached_property
    def header_row(self) -> str:
        return self.header_fmt.format(self.headers)

    def header_bar(self, char: str = '-') -> Optional[str]:
        bar = char * len(self.header_row)
        return bar[:get_terminal_size().columns] if self._stdout else bar

    @classmethod
    def auto_print_rows(cls, rows, header=True, bar=True, sort=False, sort_by=None, sort_keys=True, **kwargs):
        if len(rows) < 1:
            return
        if isinstance(rows, dict):
            rows = [row for row in rows.values()]

        keys = sorted(rows[0].keys()) if type(rows[0]) is dict and sort_keys else rows[0].keys()
        tbl = Table(
            *[Column(k, k, rows) for k in keys], auto_header=header, auto_bar=bar, sort=sort, sort_by=sort_by, **kwargs
        )
        tbl.print_rows(rows)

    @classmethod
    def auto_format_rows(cls, rows, header=True, bar=True, sort=False, sort_by=None, **kwargs):
        if len(rows) < 1:
            return
        if isinstance(rows, dict):
            rows = [row for row in rows.values()]

        keys = sorted(rows[0].keys()) if type(rows[0]) is dict else rows[0].keys()
        tbl = Table(*[Column(k, k, rows) for k in keys], sort=sort, sort_by=sort_by, **kwargs)
        output_rows = tbl.format_rows(rows)
        if header:
            if bar:
                output_rows.insert(0, tbl.header_bar())
            output_rows.insert(0, tbl.header_row.rstrip())
        return output_rows

    def _print(self, content: str, color: Union[str, int, None] = None):
        if color is not None:
            content = colored(content, color)
        self._file.write(content + '\n')
        if self._flush:
            self._file.flush()

    def print_header(self, add_bar: bool = True, color: Union[str, int, None] = None):
        self.auto_header = False
        self._print(self.header_row.rstrip(), color)
        if add_bar or self.auto_bar:
            self.print_bar(color=color)

    def print_bar(self, char: str = '-', color: Union[str, int, None] = None):
        self.auto_bar = False
        self._print(self.header_bar(char), color)

    def format_row(self, row: Row) -> str:
        """
        Format the given row using the `row_fmt` that was generated based on the columns defined for this table.

        The following error means that one of the values needs to be converted to an appropriate type, or the format
        specification needs to be fixed (e.g., formatting a list as the value when a column width was specified):
        ::
            TypeError: non-empty format string passed to object.__format__

        :param row: Mapping of {column key: row value} pairs
        :return: The formatted row
        :raises TypeError: if one of the values has a type that is incompatible with the format string
        """
        if isinstance(row, TableBar) or row is TableBar:
            return self.header_bar(row.char)
        elif isinstance(row, HeaderRow) or row is HeaderRow:
            return self.header_row

        # Don't str() the row[k] value! That will break type-specific format strings (e.g., int/float)
        row = {k: v if (v := row.get(k)) is not None else '' for k in self.keys}

        if self.has_custom_formatter:
            row_str = '  '.join(c.format(row[c.key]) for c in self.columns)
        else:
            try:
                row_str = self.row_fmt.format(row)
                if self.fix_ansi_width and ANSI_COLOR_RX.search(row_str):
                    row_str = '  '.join(c.format(row[c.key]) for c in self.columns)
            except TypeError as e:
                raise TableFormatException('row', self.row_fmt, row, e) from e
            except ValueError:
                row_str = '  '.join(c.format(row[c.key]) for c in self.columns)

        return row_str.rstrip()

    def print_row(self, row: Row, color: Union[str, int, None] = None):
        if self.auto_header:
            self.print_header(color=color)
        # Use print_header for headers, but bars can be handled by format_row
        if isinstance(row, HeaderRow) or row is HeaderRow:
            self.print_header(row.bar, color)
        else:
            self._print(self.format_row(row), color)

    def sorted(self, rows: Iterable[Row]):
        if isinstance(rows, dict):
            rows = rows.values()

        if self.sort_by is not None:
            sort_by = [self.sort_by] if not isinstance(self.sort_by, (list, tuple, set)) else self.sort_by
            try:
                rows = sorted(rows, key=replacement_itemgetter(*sort_by, replacements={None: -1}))
            except TypeError:
                rows = sorted(rows, key=replacement_itemgetter(*sort_by, replacements={None: ''}))
        elif self.sort:
            rows = sorted(rows)

        return rows

    def format_rows(self, rows: Iterable[Row], full: bool = False) -> Union[list[str], str]:
        if full:
            orig_file, orig_flush = self._file, self._flush
            self._flush = False
            self._file = sio = StringIO()
            try:
                self.print_rows(rows)
                return sio.getvalue()
            finally:
                self._file, self._flush = orig_file, orig_flush
        else:
            return [self.format_row(row) for row in self.sorted(rows)]

    def set_width(self, rows: Iterable[Row]):
        ignore = (TableBar, HeaderRow)
        for col in self.columns:
            values = (row.get(col.key) for row in rows if not isinstance(row, ignore) and row not in ignore)
            col.width = list(filter(None, values)) or 0

    def print_rows(
        self, rows: Iterable[Row], header: bool = False, update_width: bool = False, color: Union[str, int, None] = None
    ):
        rows = self.sorted(rows)
        if update_width or self.update_width:
            self.set_width(rows)

        if header or self.auto_header:
            self.print_header(color=color)
        try:
            for row in rows:
                # Use print_header for headers, but bars can be handled by format_row
                if isinstance(row, HeaderRow) or row is HeaderRow:
                    self.print_header(row.bar, color)
                else:
                    self._print(self.format_row(row), color)  # noqa
        except IOError as e:
            if e.errno == 32:  # broken pipe
                return
            raise


def _mono_width(text: str):
    return wcswidth(normalize('NFC', text))


# endregion


# region Serialization


class Printer:
    formats = ['json', 'json-compact', 'json-pretty', 'json-lines', 'yaml', 'pprint', 'table', 'plain']

    def __init__(self, output_format: str):
        if output_format is None or output_format in Printer.formats:
            self.output_format = output_format
        else:
            raise ValueError(f'Invalid output format={output_format!r} (valid options: {self.formats})')

    def pformat(self, content, *args, **kwargs):
        if isinstance(content, GeneratorType):
            return '\n'.join(self.pformat(c, *args, **kwargs) for c in content)
        elif self.output_format == 'json':
            return json.dumps(content, cls=PermissiveJSONEncoder, ensure_ascii=False)
        elif self.output_format == 'pseudo-json':
            return json.dumps(content, sort_keys=True, indent=4, cls=PseudoJsonEncoder, ensure_ascii=False)
        elif self.output_format == 'json-pretty':
            return json.dumps(content, sort_keys=True, indent=4, cls=PermissiveJSONEncoder, ensure_ascii=False)
        elif self.output_format == 'json-compact':
            return json.dumps(content, separators=(',', ':'), cls=PermissiveJSONEncoder, ensure_ascii=False)
        elif self.output_format == 'json-lines':
            if not isinstance(content, (list, set)):
                raise TypeError(f'Expected list or set; found {type(content).__name__}')
            lines = ['[']
            last = len(content) - 1
            for i, val in enumerate(content):
                suffix = ',' if i < last else ''
                lines.append(json.dumps(val, cls=PermissiveJSONEncoder, ensure_ascii=False) + suffix)
            lines.append(']\n')
            return '\n'.join(lines)
        elif self.output_format == 'plain':
            if isinstance(content, str):
                return content
            elif isinstance(content, Mapping):
                return '\n'.join('{}: {}'.format(k, v) for k, v in sorted(content.items()))
            elif all(isinstance(content, abc_type) for abc_type in (Sized, Iterable, Container)):
                return '\n'.join(sorted(map(str, content)))
            else:
                return str(content)
        elif self.output_format == 'yaml':
            return yaml_dump(
                content,
                kwargs.pop('force_single_yaml', False),
                kwargs.pop('indent_nested_lists', True),
                kwargs.pop('default_flow_style', None),
                sort_keys=kwargs.pop('sort_keys', True),
            )
        elif self.output_format == 'pprint':
            return pprint.pformat(content)
        elif self.output_format == 'table':
            try:
                return Table.auto_format_rows(content, *args, **kwargs)
            except AttributeError:
                raise ValueError(f'Invalid content format to be formatted as a {self.output_format}')
        else:
            return content

    def pprint(self, content, *args, gen_empty_error=None, **kwargs):
        if isinstance(content, GeneratorType):
            i = 0
            for c in content:
                self.pprint(c, *args, **kwargs)
                i += 1

            if (i == 0) and gen_empty_error:
                log.error(gen_empty_error)
        elif self.output_format == 'table':
            try:
                Table.auto_print_rows(content, *args, **kwargs)
            except AttributeError:
                raise ValueError(f'Invalid content format to be formatted as a {self.output_format}')
        else:
            print(self.pformat(content, *args, **kwargs))


class PermissiveJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (set, KeysView)):
            return sorted(o)
        elif isinstance(o, ValuesView):
            return list(o)
        elif isinstance(o, Mapping):
            return dict(o)
        elif isinstance(o, bytes):
            try:
                return o.decode('utf-8')
            except UnicodeDecodeError:
                return o.hex(' ', -4)
        elif isinstance(o, datetime):
            return o.strftime('%Y-%m-%d %H:%M:%S %Z')
        elif isinstance(o, date):
            return o.strftime('%Y-%m-%d')
        elif isinstance(o, (type, timedelta)):
            return str(o)
        elif isinstance(o, TracebackType):
            return ''.join(format_tb(o)).splitlines()
        elif hasattr(o, '__to_json__'):
            return o.__to_json__()
        elif hasattr(o, '__serializable__'):
            return o.__serializable__()
        return super().default(o)


class PseudoJsonEncoder(PermissiveJSONEncoder):
    def default(self, o):
        try:
            return super().default(o)
        except TypeError:
            return repr(o)
        except UnicodeDecodeError:
            return o.decode('utf-8', 'replace')


class IndentedYamlDumper(yaml.SafeDumper):
    """This indents lists that are nested in dicts in the same way as the Perl yaml library"""
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def prep_for_yaml(obj):
    if isinstance(obj, UserDict):
        obj = obj.data
    # noinspection PyTypeChecker
    if isinstance(obj, Mapping):
        return {prep_for_yaml(k): prep_for_yaml(v) for k, v in obj.items()}
    elif isinstance(obj, (set, KeysView)):
        return [prep_for_yaml(v) for v in sorted(obj)]
    elif isinstance(obj, (list, tuple, map, ValuesView)):
        return [prep_for_yaml(v) for v in obj]
    elif isinstance(obj, bytes):
        try:
            return obj.decode('utf-8')
        except UnicodeDecodeError:
            return obj.hex(' ', -4)
    elif isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M:%S %Z')
    elif isinstance(obj, date):
        return obj.strftime('%Y-%m-%d')
    elif isinstance(obj, (type, timedelta)):
        return str(obj)
    elif hasattr(obj, '__serializable__'):
        return obj.__serializable__()
    else:
        return obj


def yaml_dump(
    data, force_single_yaml: bool = False, indent_nested_lists: bool = True, default_flow_style: bool = None, **kwargs
) -> str:
    """
    Serialize the given data as YAML

    :param data: Data structure to be serialized
    :param force_single_yaml: Force a single YAML document to be created instead of multiple ones when the
      top-level data structure is not a dict
    :param indent_nested_lists: Indent lists that are nested in dicts in the same way as the Perl yaml library
    :param default_flow_style: Whether the default flow style should be used
    :return: Yaml-formatted data
    """
    content = prep_for_yaml(data)
    kwargs.setdefault('explicit_start', True)
    kwargs.setdefault('width', float('inf'))
    kwargs.setdefault('allow_unicode', True)
    if indent_nested_lists:
        kwargs['Dumper'] = IndentedYamlDumper

    if isinstance(content, (dict, str)) or force_single_yaml:
        # kwargs.setdefault('default_flow_style', False if default_flow_style is None else default_flow_style)
        # formatted = yaml.dump(content, **kwargs)
        return _dump_yaml(content, kwargs, default_flow_style)
    else:
        # kwargs.setdefault('default_flow_style', True if default_flow_style is None else default_flow_style)
        # formatted = yaml.dump_all(content, **kwargs)
        return '\n'.join(_dump_yaml(row, kwargs, default_flow_style) for row in content)

    # if formatted.endswith('...\n'):
    #     formatted = formatted[:-4]
    # if formatted.endswith('\n'):
    #     formatted = formatted[:-1]
    # return formatted


def _dump_yaml(content, kwargs, default_flow_style: bool = None) -> str:
    kwargs.setdefault('default_flow_style', False if default_flow_style is None else default_flow_style)
    formatted = yaml.dump(content, **kwargs)
    if formatted.endswith('...\n'):
        formatted = formatted[:-4]
    if formatted.endswith('\n'):
        formatted = formatted[:-1]
    return formatted

# endregion
