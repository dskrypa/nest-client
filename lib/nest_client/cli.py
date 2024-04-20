"""
:author: Doug Skrypa
"""

from __future__ import annotations

import logging
from abc import ABC
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli_command_parser import AsyncCommand, SubCommand, Positional, Option, Flag, Counter, main

from .output import Printer, Table, SimpleColumn, colored, cdiff

if TYPE_CHECKING:
    from nest_client.client import NestWebClient
    from nest_client.entities import Schedule

log = logging.getLogger(__name__)
SHOW_ITEMS = ('energy', 'weather', 'buckets', 'bucket_names', 'schedule')


class NestCLI(AsyncCommand, description='Nest Thermostat Manager', option_name_mode='*-'):
    action = SubCommand()
    config = Option('-c', default='~/.config/nest.cfg', metavar='PATH', help='Config file location')
    reauth = Flag('-A', help='Force re-authentication, even if a cached session exists')
    verbose = Counter('-v', help='Increase logging verbosity (can specify multiple times)')

    def _init_command_(self):
        log_fmt = '%(asctime)s %(levelname)s %(name)s %(lineno)d %(message)s' if self.verbose else '%(message)s'
        logging.basicConfig(level=logging.DEBUG if self.verbose else logging.INFO, format=log_fmt)
        logging.getLogger('httpx').setLevel(logging.WARNING)

    def client(self):
        from nest_client.client import NestWebClient

        return NestWebClient(self.config, self.reauth)

    async def get_thermostat(self, nest: NestWebClient):
        from nest_client.entities import ThermostatDevice

        return await ThermostatDevice.find(nest)


class Status(NestCLI, help='Show current status'):
    format = Option('-f', default='yaml', choices=Printer.formats, help='Output format')
    details = Flag('-d', help='Show more detailed information')

    async def main(self):
        async with self.client() as nest:
            await self.show_status(nest)

    async def show_status(self, nest: NestWebClient):
        from nest_client.entities import ThermostatDevice

        device = await ThermostatDevice.find(nest)
        if self.details:
            shared = await device.get_shared()
            status = {'device': device.value, 'shared': shared.value}
            if nest.config.temp_unit == 'f':
                _convert_temp_values(status)
            Printer(self.format).pprint(status)
        else:
            mode = device.schedule_mode.upper()
            tbl = Table(
                SimpleColumn('Humidity'),
                SimpleColumn('Mode', len(mode)),
                SimpleColumn('Fan', 7),
                SimpleColumn('Target', display=mode != 'RANGE'),
                SimpleColumn('Target (low)', display=mode == 'RANGE'),
                SimpleColumn('Target (high)', display=mode == 'RANGE'),
                SimpleColumn('Temperature'),
                fix_ansi_width=True,
            )

            shared = await device.get_shared()
            current = shared.current_temperature
            target = shared.target_temperature
            target_lo, target_hi = shared.target_temp_range
            status_table = {
                'Mode': colored(mode, 14 if mode == 'COOL' else 13 if mode == 'RANGE' else 9),
                'Humidity': device.humidity,
                'Temperature': colored(f'{current:>11.1f}', 11),
                'Fan': colored('RUNNING', 10) if shared.running else colored('OFF', 8),
                'Target (low)': colored(f'{target_lo:>12.1f}', 14 if target_lo < current else 9),
                'Target (high)': colored(f'{target_hi:>13.1f}', 14 if target_hi < current else 9),
                'Target': colored(f'{target:>6.1f}', 14 if target < current else 9),
            }
            tbl.print_rows([status_table])


class Temp(NestCLI, help='Set a new temperature'):
    temp: float = Positional(help='The temperature to set')
    only_set = Flag('-s', help='Only set the temperature - do not force it to run if the delta is < 0.5 degrees')

    async def main(self):
        async with self.client() as nest:
            thermostat = await self.get_thermostat(nest)
            shared = await thermostat.get_shared()
            if self.only_set:
                await shared.set_temp(self.temp)
            else:
                await shared.set_temp_and_force_run(self.temp)


class Range(NestCLI, help='Set a new temperature range'):
    low: float = Positional(help='The low temperature to set')
    high: float = Positional(help='The high temperature to set')

    async def main(self):
        async with self.client() as nest:
            thermostat = await self.get_thermostat(nest)
            shared = await thermostat.get_shared()
            await shared.set_temp_range(self.low, self.high)


class Mode(NestCLI, help='Change the current mode'):
    mode = Positional(choices=('cool', 'heat', 'range', 'off'), help='The mode to set')

    async def main(self):
        async with self.client() as nest:
            thermostat = await self.get_thermostat(nest)
            shared = await thermostat.get_shared()
            await shared.set_mode(self.mode)


class Fan(NestCLI, help='Turn the fan on or off'):
    state = Positional(choices=('on', 'off'), help='The fan state to change to')
    duration: int = Option(
        '-d', default=1800, help='Time (in seconds) for the fan to run (ignored if setting state to off)'
    )

    async def main(self):
        async with self.client() as nest:
            thermostat = await self.get_thermostat(nest)
            if self.state == 'on':
                await thermostat.start_fan(self.duration)
            elif self.state == 'off':
                await thermostat.stop_fan()


class Show(NestCLI, help='Show information'):
    item = Positional(choices=SHOW_ITEMS, help='The information to show')
    buckets = Positional(nargs='*', help='The buckets to show (only applies to item=buckets)')
    format = Option('-f', default='yaml', choices=Printer.formats, help='Output format')
    raw = Flag('-r', help='Show the full raw response instead of the processed response (only applies to item=buckets)')

    async def main(self):
        async with self.client() as nest:
            if self.item == 'schedule':
                schedule = await nest.get_object('schedule')  # type: Schedule
                schedule.weekly_schedule.print(self.format or ('raw' if self.raw else 'table'), self.raw)
            else:
                if self.item == 'energy':
                    data = (await nest.get_object('energy_latest')).value
                elif self.item == 'weather':
                    data = await nest.get_weather()
                elif self.item == 'buckets':
                    data = await nest.app_launch(self.buckets)
                    if not self.raw:
                        data = data['updated_buckets']
                elif self.item == 'bucket_names':
                    bucket = await nest.get_object('buckets')
                    data = {obj.type: names for obj, names in (await bucket.types_by_parent()).items()}
                else:
                    raise ValueError(f'Unexpected item={self.item}')

                Printer(self.format or 'yaml').pprint(data)


# region Schedule Commands


class ScheduleCmd(NestCLI, choice='schedule', help='Update the schedule'):
    sub_action = SubCommand()
    dry_run = Flag('-D', help='Print actions that would be taken instead of taking them')


class AddOrRemoveSchedule(ScheduleCmd, ABC):
    cron = Positional(help='Cron-format schedule to use')
    temp = Positional(type=float, help='The temperature to set at the specified time')
    unit = Option('-u', choices=('f', 'c'), help='Input unit (default: from config)')

    async def update_schedule(self, action: str):
        from nest_client.entities import Schedule

        async with self.client() as nest:
            schedule = await Schedule.find(nest)
            await schedule.update(self.cron, action, self.temp, self.unit, dry_run=self.dry_run)


class Add(AddOrRemoveSchedule, help='Add entries with the specified schedule'):
    async def main(self):
        await self.update_schedule('add')


class Remove(AddOrRemoveSchedule, help='Remove entries with the specified schedule'):
    async def main(self):
        await self.update_schedule('remove')


class Save(ScheduleCmd, help='Save the current schedule to a file'):
    path = Positional(help='The path to a file in which the current schedule should be saved')
    overwrite = Flag('-W', help='Overwrite the file if it already exists')

    async def main(self):
        from nest_client.entities import Schedule

        async with self.client() as nest:
            schedule = await Schedule.find(nest)
            await schedule.save(self.path, self.overwrite, self.dry_run)


class Load(ScheduleCmd, help='Load a schedule from a file'):
    path = Positional(help='The path to a file containing the schedule that should be loaded')
    force = Flag('-F', help='Force the schedule to be pushed, even if it matches the current schedule')

    async def main(self):
        from nest_client.entities import Schedule

        async with self.client() as nest:
            schedule = await Schedule.from_file(nest, self.path)
            await schedule.push(force=self.force, dry_run=self.dry_run)


class ShowSchedule(ScheduleCmd, choice='show', help='Show the current schedule'):
    format = Option('-f', choices=Printer.formats, help='Output format')
    raw = Counter('-r', help='Show the schedule in the Nest format instead of readable')
    unit = Option('-u', choices=('f', 'c'), help='Display unit (default: from config)')

    async def main(self):
        from nest_client.entities import Schedule

        async with self.client() as nest:
            schedule = await Schedule.find(nest)
            schedule.print(self.format or ('yaml' if self.raw else 'table'), self.raw, self.unit, self.raw > 1)


# endregion


class FullStatus(NestCLI, help='Show/save the full device+shared status'):
    path = Option('-p', help='Location to store status info')
    diff = Flag('-d', help='Print a diff of the current status compared to the previous most recent status')

    async def main(self):
        import json
        import time

        path = Path(self.path or '~/etc/nest/status').expanduser()
        if path.exists() and not path.is_dir():
            raise ValueError(f'Invalid {path=} - it must be a directory')
        elif not path.exists():
            path.mkdir(parents=True)

        async with self.client() as nest:
            data = await nest.app_launch(['device', 'shared'])
            status_path = path.joinpath(f'status_{int(time.time())}.json')
            log.info(f'Saving status to {status_path.as_posix()}')
            with status_path.open('w', encoding='utf-8', newline='\n') as f:
                json.dump(data, f, indent=4, sort_keys=True)

            if self.diff:
                latest = max((p for p in path.iterdir() if p != status_path), key=lambda p: p.stat().st_mtime)
                cdiff(latest.as_posix(), status_path.as_posix())


# region Config Commands


class Config(NestCLI, help='Manage configuration'):
    sub_action = SubCommand()


class ShowConfig(Config, choice='show', help='Show the config file contents'):
    async def main(self):
        log.warning(
            'WARNING: The [oauth] section contains credentials that should be kept secret - do not share this'
            ' output with anyone\n',
            extra={'color': 'red'},
        )

        async with self.client() as nest:
            with nest.config.path.open('r') as f:
                print(f.read())


class Set(Config, help='Set configs'):
    section = Positional(choices=('credentials', 'device', 'oauth', 'units'), help='The section to modify')
    key = Positional(help='The key within the specified section to modify')
    value = Positional(help='The new value for the specified section and key')

    async def main(self):
        async with self.client() as nest:
            nest.config.maybe_set(self.section, self.key, self.value)


# endregion


def _convert_temp_values(status: dict[str, dict[str, Any]]):
    from nest_client.utils import celsius_to_fahrenheit as c2f

    temp_key_map = {
        'shared': ('target_temperature_high', 'target_temperature_low', 'target_temperature', 'current_temperature'),
        'device': ('backplate_temperature', 'leaf_threshold_cool'),
    }
    for section, keys in temp_key_map.items():
        for key in keys:
            status[section][key] = c2f(status[section][key])


if __name__ == '__main__':
    main()
