# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for the pin commands that run_shell() adds to the interactive shell."""

import pytest

from pytactl import debugboard, shell

PIN_MASK = 0x08  # pin_number 3


class _FakeBoard:
    def __init__(self, pin):
        self.pins = {pin.pin_number: pin}
        self.quick_methods = {}


@pytest.fixture
def ftdi_shell(monkeypatch):
    """An AlpacaCmd with a 'pkey' command driving FTDI pin A3."""
    pin = debugboard.FtdiPin(
        None, {"bus": "A", "command": "pkey", "initial_value": "0", "pin_number": "3"}
    )
    pin.setPort(debugboard.FtdiPort("A", "S1"))

    # run_shell() adds do_/help_ methods to the class; let monkeypatch remove them.
    monkeypatch.setattr(shell.AlpacaCmd, "do_pkey", None, raising=False)
    monkeypatch.setattr(shell.AlpacaCmd, "help_pkey", None, raising=False)
    monkeypatch.setattr(shell.AlpacaCmd, "cmdloop", lambda self: None)
    monkeypatch.setattr(shell, "_create_board", lambda *a, **k: _FakeBoard(pin))
    shell.run_shell()
    return shell.AlpacaCmd(), pin.port


def test_shell_pin_command_sets_and_clears_ftdi_pin(ftdi_shell):
    cmd, port = ftdi_shell
    cmd.onecmd("pkey 1")
    assert port.status & PIN_MASK
    cmd.onecmd("pkey 0")
    assert not port.status & PIN_MASK


def test_shell_pin_command_without_value_sets_low(ftdi_shell):
    cmd, port = ftdi_shell
    cmd.onecmd("pkey 1")
    cmd.onecmd("pkey")
    assert not port.status & PIN_MASK


def test_shell_pin_command_rejects_non_integer(ftdi_shell, caplog):
    cmd, port = ftdi_shell
    cmd.onecmd("pkey x")
    assert not port.status & PIN_MASK
    assert "must be an integer" in caplog.text
