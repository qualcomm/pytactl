# Copyright (c) 2025-2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

import logging
import os
import sys
from argparse import ArgumentParser

from . import (
    BUNDLED_DEFAULT_CONFIG_FILENAME,
    DEFAULT_CONFIG_FILENAME,
    DEFAULT_CONFIG_REPOSITORY,
    INSTALLED_TAC_CONFIG_PATH,
    PACKAGE_TAC_CONFIG_PATH,
    __version__,
    default_tac_config_path,
)

logger = logging.getLogger()


def _setup_logging(level):
    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def build_parser():
    parser = ArgumentParser(
        prog="pytactl",
        description="Test Automation Controller (TAC/Alpaca) for Qualcomm debug boards.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    # Options shared by every subcommand.
    base = ArgumentParser(add_help=False)
    base.add_argument("--log-level", default="DEBUG", help="Log level (default: DEBUG)")

    # Options shared by the board-driving subcommands.
    common = ArgumentParser(add_help=False)
    common.add_argument(
        "--serial",
        nargs="+",
        help="Debug board serial number(s)",
    )
    # Where to load the TAC configs from.
    config_path = ArgumentParser(add_help=False)
    config_path.add_argument(
        "--tac-config-path",
        help="Path to directory with TAC configs (devicelist.json + "
        ".pinout.json files). Defaults to the configs bundled with the "
        "package, which cover every board upstream ships a config for, unless "
        "the deprecated 'installconfigs' has installed a set.",
    )

    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="COMMAND")

    subparsers.add_parser(
        "list",
        parents=[base],
        help="List connected debug boards and their serial numbers",
    )

    shell = subparsers.add_parser(
        "shell", parents=[base, common, config_path], help="Run the interactive shell"
    )
    shell.add_argument(
        "--config-file-path",
        help="Path to a single config file (.pinout.json, or a .tcnf in either "
        "the split or the legacy combined format); use for debugging the config "
        "file syntax.",
    )

    oneshot = subparsers.add_parser(
        "oneshot",
        parents=[base, common, config_path],
        help="Run a single command and exit",
    )
    oneshot.add_argument("command", help="Command to run, e.g. bootToEDL")
    oneshot.add_argument(
        "value",
        nargs="?",
        help="Optional integer value for pin commands, e.g. 1",
    )
    oneshot.add_argument(
        "--config-file-path",
        help="Path to a single config file (.pinout.json, or a .tcnf in either "
        "the split or the legacy combined format); use for debugging the config "
        "file syntax.",
    )

    subparsers.add_parser(
        "listconfigs",
        parents=[base, config_path],
        help="List the TAC configs found in the config directory and check "
        "that every devicelist.json entry has a loadable config",
    )

    installconfigs = subparsers.add_parser(
        "installconfigs",
        parents=[base],
        help="(deprecated) Download TAC config files from the config repository "
        "and convert them to the shared pinout format. The configs bundled with "
        "pytactl carry fixes upstream does not; use 'convertconfigs' and "
        "--tac-config-path for a config set of your own",
    )
    installconfigs.add_argument(
        "--config-repository",
        default=DEFAULT_CONFIG_REPOSITORY,
        help="Config repository URL to fetch configs from (default: %(default)s)",
    )
    installconfigs.add_argument(
        "--local-path",
        help="Directory to install configs into (default: the platformdirs "
        "user data directory for pytactl)",
    )
    installconfigs.add_argument(
        "--ref",
        default="HEAD",
        help="Git ref (branch, tag, or commit) to fetch from (default: %(default)s)",
    )
    installconfigs.add_argument(
        "--repository-path",
        default="configurations",
        help="Path within the repository holding the configs (default: %(default)s)",
    )
    installconfigs.add_argument(
        "--no-annotate",
        action="store_true",
        help="Do not record the repository, ref and commit the configs were "
        "imported from in each converted config",
    )

    convertconfigs = subparsers.add_parser(
        "convertconfigs",
        parents=[base],
        help="Convert a directory of TAC config files to the shared pinout "
        "format (.pinout.json)",
    )
    convertconfigs.add_argument(
        "source",
        help="Directory holding the TAC config files to convert (e.g. the "
        "'configurations' directory of a qcom-test-automation-controller "
        "checkout)",
    )
    convertconfigs.add_argument(
        "--output",
        help="Directory to write the converted configs into (default: convert "
        "in place)",
    )
    convertconfigs.add_argument(
        "--write-overlay",
        action="store_true",
        help="Also write the slim UI overlay (.tcnf) beside each .pinout.json, "
        "producing the complete two-file layout instead of only the half "
        "pytactl needs",
    )
    convertconfigs.add_argument(
        "--default-config",
        default=DEFAULT_CONFIG_FILENAME,
        help="File name that devicelist.json entries with no config of their "
        "own should point at (default: %(default)s). The config set bundled "
        "with pytactl keeps the FTDI Alpaca-Lite default under its own name "
        f"rather than a copy, so refreshing it passes {BUNDLED_DEFAULT_CONFIG_FILENAME} here.",
    )
    convertconfigs.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be converted without writing anything",
    )
    convertconfigs.add_argument(
        "--no-annotate",
        action="store_true",
        help="Do not record the repository and commit of the source checkout in "
        "each converted config. Annotated configs carry an extra 'source' key, "
        "which upstream's pinout schema does not allow; use this for configs "
        "that must validate against it unchanged",
    )

    service = subparsers.add_parser(
        "service", parents=[base, common, config_path], help="Run the REST API service"
    )
    service.add_argument(
        "--hostname",
        default="0.0.0.0",
        help="Host name the server attaches to (default: 0.0.0.0)",
    )
    service.add_argument(
        "--port",
        default=5000,
        type=int,
        help="Port on the host to attach to (default: 5000)",
    )
    return parser


def _list_boards():
    from .debugboard import Board

    boards = Board.list_boards()
    if not boards:
        print("No connected debug boards found.")
        return

    print("Connected debug boards:")
    for board in boards:
        serial = board["serial"] or "<no serial reported>"
        vid_pid = f"{board['vid']:04x}:{board['pid']:04x}"
        print(f"  {board['type']:<14} vid:pid={vid_pid}  serial={serial}")


def _list_configs(tac_config_path):
    from .tacconfig import list_directory

    if os.path.realpath(tac_config_path) == os.path.realpath(PACKAGE_TAC_CONFIG_PATH):
        origin = "bundled with pytactl"
    elif os.path.realpath(tac_config_path) == os.path.realpath(
        INSTALLED_TAC_CONFIG_PATH
    ):
        origin = "installed by installconfigs"
    else:
        origin = "set with --tac-config-path"
    print(f"TAC config directory: {tac_config_path} ({origin})")

    try:
        listing = list_directory(tac_config_path)
    except (OSError, ValueError) as error:
        print(f"Cannot read devicelist.json: {error}")
        return False

    entries = listing["entries"]
    if not entries:
        print("devicelist.json lists no debug boards.")
        return False

    name_width = max(len(entry["name"]) for entry in entries)
    config_width = max(len(entry["configPath"]) for entry in entries)
    print(f"Configs for {len(entries)} debug boards:")
    for entry in entries:
        line = (
            f"  {entry['name']:<{name_width}}  {entry['configPath']:<{config_width}}"
            f"  platform_id={entry['platform_id']}"
        )
        if entry["usb_descriptor"]:
            line += f"  usb_descriptor={entry['usb_descriptor']!r}"
        if entry["status"] != "ok":
            line += f"  {entry['status'].upper()}: {entry['error']}"
        print(line)

    if listing["unreferenced"]:
        print("Config files not referenced by devicelist.json:")
        for name in listing["unreferenced"]:
            print(f"  {name}")

    broken = [entry for entry in entries if entry["status"] != "ok"]
    if broken:
        print(f"{len(broken)} debug board(s) have a missing or invalid config")
        return False
    return True


def _single_board_serial(boards):
    if not boards:
        return None, "no connected debug boards found"

    if len(boards) > 1:
        serials = ", ".join(
            board.get("serial") or "<no serial reported>" for board in boards
        )
        return None, f"multiple connected debug boards found: {serials}"

    serial = boards[0].get("serial")
    if not serial:
        return None, "the connected debug board did not report a serial number"

    return serial, None


def _resolve_serial(args, parser, command):
    if args.serial:
        return args.serial[0]

    if args.config_file_path:
        return None

    from .debugboard import Board

    serial, error = _single_board_serial(Board.list_boards())
    if serial:
        logger.info("Using only connected debug board serial %s", serial)
        return serial

    parser.error(f"{command} requires --serial or --config-file-path ({error})")


def _resolve_tac_config_path(args):
    if getattr(args, "tac_config_path", False) is not None:
        return

    args.tac_config_path = default_tac_config_path()
    if args.tac_config_path == INSTALLED_TAC_CONFIG_PATH:
        logger.warning(
            "Using the TAC configs installed by the deprecated 'installconfigs' "
            "in %s instead of the configs bundled with pytactl. The installed "
            "configs lack the fixes the bundled ones carry and are not updated "
            "with pytactl; delete %s to use the bundled configs, or pass "
            "--tac-config-path to choose a config directory explicitly.",
            INSTALLED_TAC_CONFIG_PATH,
            INSTALLED_TAC_CONFIG_PATH,
        )


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    _resolve_tac_config_path(args)

    if args.mode == "list":
        _list_boards()
    elif args.mode == "listconfigs":
        if not _list_configs(args.tac_config_path):
            sys.exit(1)
    elif args.mode == "installconfigs":
        from .installconfigs import install_configs

        install_configs(
            args.config_repository,
            args.local_path,
            args.ref,
            args.repository_path,
            not args.no_annotate,
        )
    elif args.mode == "convertconfigs":
        from .installconfigs import convert_configs

        convert_configs(
            args.source,
            args.output,
            args.write_overlay,
            args.dry_run,
            not args.no_annotate,
            args.default_config,
        )
    elif args.mode == "service":
        if not args.serial:
            parser.error("service requires --serial")
        from .service import run_service

        run_service(args.serial, args.tac_config_path, args.hostname, args.port)
    elif args.mode == "oneshot":
        from .shell import run_oneshot

        serial = _resolve_serial(args, parser, "oneshot")
        run_oneshot(
            args.command,
            serial,
            args.config_file_path,
            args.tac_config_path,
            args.value,
        )
    else:  # shell
        from .shell import run_shell

        serial = _resolve_serial(args, parser, "shell")
        run_shell(serial, args.config_file_path, args.tac_config_path)


if __name__ == "__main__":
    main()
