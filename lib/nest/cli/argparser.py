"""
:author: Doug Skrypa
"""

from argparse import ArgumentParser
from contextlib import suppress


class ArgParser(ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__constants = {}

    def add_constant(self, key, value):
        self.__constants[key] = value

    def _get_subparser(self, dest: str):
        try:
            return next((sp for sp in self._subparsers._group_actions if sp.dest == dest), None)
        except AttributeError:  # If no subparsers exist yet
            return None

    def add_subparser(self, dest: str, name: str, help_desc: str = None, **kwargs) -> 'ArgParser':
        """
        Add a subparser for a subcommand to the subparser group with the given destination variable name.  Creates the
        group if it does not already exist.

        :param dest: The subparser group destination for this subparser
        :param name: The name of the subcommand/subparser to add
        :param help_desc: The text to be used as both the help and description for this subcommand
        :param kwargs: Keyword args to pass to the :func:`add_parser` function
        :return: The parser that was created
        """
        sp_group = self._get_subparser(dest) or self.add_subparsers(dest=dest, title='subcommands')
        sub_parser = sp_group.add_parser(
            name, help=kwargs.pop('help', help_desc), description=kwargs.pop('description', help_desc), **kwargs
        )
        return sub_parser  # noqa

    def parse_args(self, *args, **kwargs):
        args = super().parse_args(*args, **kwargs)
        with suppress(AttributeError):
            if missing := next((sp for sp in self._subparsers._group_actions if getattr(args, sp.dest) is None), None):
                self.error(f'missing required positional argument: {missing.dest} (use --help for more details)')
        args.__dict__.update(self.__constants)
        update_subparser_constants(self, args)
        return args

    def get_subparsers(self):
        try:
            return {sp.dest: sp for sp in self._subparsers._group_actions}
        except AttributeError:
            return {}

    def _add_arg_to_subparsers(self, args, kwargs, subparsers=None):
        if subparsers is None:
            subparsers = self.get_subparsers()
        for subparser in {val for sp in subparsers.values() for val in sp.choices.values()}:
            subparser.add_common_arg(*args, **kwargs)

    def add_common_sp_arg(self, *args, **kwargs):
        """Add an argument with the given parameters to every subparser in this ArgParser, or itself if it has none"""
        if subparsers := self.get_subparsers():
            self._add_arg_to_subparsers(args, kwargs, subparsers)
        else:
            self.add_argument(*args, **kwargs)

    def add_common_arg(self, *args, **kwargs):
        """Add an argument with the given parameters to this ArgParser and every subparser in it"""
        self.add_argument(*args, **kwargs)
        if subparsers := self.get_subparsers():
            self._add_arg_to_subparsers(args, kwargs, subparsers)

    def __enter__(self):
        """Allow using ArgParsers as context managers to help organize large subparser sections when defining parsers"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return


def update_subparser_constants(parser: ArgParser, parsed):
    for dest, subparsers in parser.get_subparsers().items():
        chosen_sp = parsed.__dict__[dest]
        for sp_name, subparser in subparsers.choices.items():
            if sp_name == chosen_sp:
                parsed.__dict__.update(subparser._ArgParser__constants)
                update_subparser_constants(subparser, parsed)
