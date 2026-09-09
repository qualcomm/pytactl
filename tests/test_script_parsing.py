# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for ``Board.parse_script``: turning the Alpaca automation script in a
board config into callable methods.

The language is flat - a ``def`` at column 0 and its body one tab in, with no
nesting anywhere in the upstream config set. pytactl requires that tab rather
than accepting whatever whitespace happens to be there, so that a stray
space-indented line is reported against the config instead of being quietly
absorbed; ``pytactl convertconfigs`` normalises a config directory to tabs
(see ``tests/test_tacconfig.py``).

These use small synthetic configs rather than the bundled set: they pin down the
parser's behaviour, not any particular board.
"""

import json
import logging

import pytest

from pytactl import debugboard


def write_config(tmp_path, script, commands=("battery", "pkey"), variables=None):
    """Write a minimal pinout config driving ``commands`` and return its path."""
    config = {
        "format": "tac-pinout",
        "schema_version": "1.0",
        "platform_type": "PSOC",
        "platform_id": 999,
        "name": "Test Board",
        "description": "Test Board",
        "usb_descriptor": "",
        "reset_enabled": False,
        "pins": [
            {
                "pin_number": str(number),
                "command": command,
                "inverted": False,
                "initial_value": False,
                "initialization_priority": 0,
                "classic_action": "",
            }
            for number, command in enumerate(commands)
        ],
        "variables": variables or [],
        "script": script,
    }
    path = tmp_path / "TAC_PSOC_999.pinout.json"
    path.write_text(json.dumps(config))
    return str(path)


# --------------------------------------------------------------------------
# Indentation
# --------------------------------------------------------------------------


TAB = "def powerOn()\n\tbattery 1\n\tpkey 1\n"
SPACES = "def powerOn()\n    battery 1\n    pkey 1\n"
MIXED = "def powerOn()\n\tbattery 1\n                        pkey 1\n"
DEEP = "def powerOn()\n\t\t\tbattery 1\n\tpkey 1\n"


def test_tab_indented_script_parses(tmp_path):
    board = debugboard.Board.create_from_config(write_config(tmp_path, TAB))

    assert "powerOn" in board.quick_methods
    board.powerOn()
    assert board.pins["0"].value == 1
    assert board.pins["1"].value == 1


@pytest.mark.parametrize(
    ("name", "script", "lines"),
    [
        ("spaces", SPACES, "2, 3"),
        ("mixed", MIXED, "3"),
        ("over-indented", DEEP, "2"),
    ],
)
def test_non_tab_indentation_is_rejected(name, script, lines, tmp_path):
    """Anything but a single tab is a config defect, reported against the
    config and naming the lines - not absorbed. Three upstream configs indent
    the odd line with spaces (TAC_FTDI_51, TAC_FTDI_52, TAC_FTDI_77); the
    converter re-indents them on import so the shipped configs are clean."""
    path = write_config(tmp_path, script)

    with pytest.raises(debugboard.ConfigScriptError) as excinfo:
        debugboard.Board.create_from_config(path)

    message = str(excinfo.value)
    assert f"line(s) {lines}" in message
    assert "single tab" in message
    assert "convertconfigs" in message


def test_crlf_line_endings_parse(tmp_path):
    """Some configs (the PIC32CX one) use CRLF throughout; the indentation check
    must read those lines the same way the rest of the parser does, and not
    mistake a carriage return for stray indentation."""
    script = "def powerOn()\r\n\tbattery 1\r\n\tpkey 1\r\n"

    board = debugboard.Board.create_from_config(write_config(tmp_path, script))

    assert "powerOn" in board.quick_methods
    board.powerOn()
    assert board.pins["0"].value == 1


def test_blank_lines_are_not_statements(tmp_path):
    """A whitespace-only line is not a statement, so it is not held to the tab
    rule however it is made up - 52 lines in the shipped configs are a bare
    tab, and a run of spaces is no worse."""
    script = "def powerOn()\n\tbattery 1\n   \n\tpkey 1\n"

    board = debugboard.Board.create_from_config(write_config(tmp_path, script))

    board.powerOn()
    assert board.pins["1"].value == 1


# --------------------------------------------------------------------------
# Script variables
# --------------------------------------------------------------------------


def test_declared_variables_are_substituted(tmp_path):
    path = write_config(
        tmp_path,
        "def bootToEDL()\n\tbattery 1\n\tdelay $edl\n",
        variables=[{"name": "edl", "default_value": "1300"}],
    )

    board = debugboard.Board.create_from_config(path)

    assert "bootToEDL" in board.quick_methods
    board.bootToEDL()


def test_undeclared_variable_is_reported_by_name(tmp_path):
    """TAC_FTDI_72 drives $edl/$uefi/$fastboot but declares no variables. There
    is nothing to substitute and the delay is a board timing only the config can
    supply, so say exactly that instead of letting a bare '$edl' reach exec()
    as a syntax error."""
    path = write_config(tmp_path, "def bootToEDL()\n\tbattery 1\n\tdelay $edl\n")

    with pytest.raises(debugboard.ConfigScriptError) as excinfo:
        debugboard.Board.create_from_config(path)

    message = str(excinfo.value)
    assert "$edl" in message
    assert "declares none" in message


def test_undeclared_variable_names_the_declared_ones(tmp_path):
    path = write_config(
        tmp_path,
        "def bootToEDL()\n\tdelay $edl\n\tdelay $uefi\n",
        variables=[{"name": "edl", "default_value": "1300"}],
    )

    with pytest.raises(debugboard.ConfigScriptError) as excinfo:
        debugboard.Board.create_from_config(path)

    assert "$uefi" in str(excinfo.value)
    assert "$edl" not in str(excinfo.value)


# --------------------------------------------------------------------------
# Commands the script drives but no pin implements
# --------------------------------------------------------------------------


def test_unbound_command_is_warned_about_at_load(tmp_path, caplog):
    """Several configs drive a command no pin defines - a phone script on a
    board with no such line (TAC_FTDI_23 has no power key, TAC_FTDI_29 is an RF
    switch box). It only shows up as an AttributeError from inside the script
    when someone tries to power the board on, so name it at load time."""
    path = write_config(
        tmp_path, "def powerOn()\n\tbattery 1\n\tvolup 1\n", commands=("battery",)
    )

    with caplog.at_level(logging.WARNING):
        debugboard.Board.create_from_config(path)

    assert "'volup'" in caplog.text
    assert "no pin in this config defines" in caplog.text


def test_no_warning_when_every_command_is_bound(tmp_path, caplog):
    path = write_config(tmp_path, "def powerOn()\n\tbattery 1\n\tpkey 1\n")

    with caplog.at_level(logging.WARNING):
        debugboard.Board.create_from_config(path)

    assert "no pin in this config defines" not in caplog.text


def test_language_statements_are_not_mistaken_for_commands(tmp_path, caplog):
    """delay and logComment are the language's own statements, and a bare name
    calls another function; none of them is a pin command."""
    script = (
        "def powerOff()\n\tbattery 0\n"
        "def powerOn()\n\tpowerOff\n\tdelay 500\n\tlogComment powering on\n\tbattery 1\n"
    )
    path = write_config(tmp_path, script, commands=("battery",))

    with caplog.at_level(logging.WARNING):
        board = debugboard.Board.create_from_config(path)

    assert "no pin in this config defines" not in caplog.text
    board.powerOn()


# --------------------------------------------------------------------------
# How a config defect reaches the user
# --------------------------------------------------------------------------


def test_cli_reports_a_config_defect_without_a_traceback(tmp_path, caplog):
    """A config that breaks the format is a routine thing to hit when pointing
    --config-file-path at an unconverted upstream file, so it exits with the
    message rather than a traceback."""
    from pytactl import shell

    path = write_config(tmp_path, SPACES)

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as excinfo:
        shell.run_oneshot("powerOn", config_file_path=path)

    assert excinfo.value.code == 1
    assert "single tab" in caplog.text
    assert "convertconfigs" in caplog.text
