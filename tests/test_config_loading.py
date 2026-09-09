# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests that every config file can be loaded through the USB-description based
dispatch in ``pytactl.debugboard`` (the same path used by ``pytactl.shell`` and
``pytactl.service``) and exposes the required quick methods: powerOn, powerOff,
bootToEDL.

Special-cased configs (excluded boards, known-broken configs, configs that omit
a function or reference a disabled pin) are declared centrally in conftest.py.
"""

import os

import pytest
from conftest import (
    XFAIL_EXECUTE,
    XFAIL_LOAD,
    XFAIL_REQUIRED,
    config_params,
    discover_configs,
    load_board,
    requires_bundled_configs,
    requires_configs,
)

# Functions that every config script is expected to define (unless explicitly
# documented otherwise in XFAIL_REQUIRED).
REQUIRED_FUNCTIONS = ("powerOn", "powerOff", "bootToEDL")


@requires_configs
def test_configs_exist():
    """Guard against silently testing nothing if the config dir is present but
    holds no testable configs.

    Skipped outright when the external config set is unavailable (e.g. a distro
    build environment); see ``resolve_config_dir`` in conftest.py.
    """
    assert discover_configs(), "no testable .pinout.json config files found"


@pytest.mark.parametrize("config_path", config_params(XFAIL_LOAD))
def test_config_loads_via_usb_dispatch(
    config_path, prepared_configs, patch_usb_find, monkeypatch
):
    """Each config loads cleanly when its matching USB device is plugged in."""
    config_dir, entries = prepared_configs
    entry = entries[os.path.basename(config_path)]

    board = load_board(config_path, config_dir, entries, patch_usb_find, monkeypatch)

    assert board is not None
    # The board was built from the config we asked for.
    assert board.full_config["platform_type"] == entry.platform_type


@pytest.mark.parametrize("config_path", config_params({**XFAIL_LOAD, **XFAIL_REQUIRED}))
def test_required_functions_available(
    config_path, prepared_configs, patch_usb_find, monkeypatch
):
    """powerOn, powerOff and bootToEDL are exposed as quick methods and as
    callable attributes on every board built from a config file."""
    config_dir, entries = prepared_configs
    entry = entries[os.path.basename(config_path)]

    board = load_board(config_path, config_dir, entries, patch_usb_find, monkeypatch)

    for name in REQUIRED_FUNCTIONS:
        assert name in board.quick_methods, (
            f"{entry.name}: config script is missing required function '{name}'"
        )
        assert callable(getattr(board, name, None)), (
            f"{entry.name}: '{name}' is not a callable bound method"
        )


@pytest.mark.parametrize("config_path", config_params({**XFAIL_LOAD, **XFAIL_EXECUTE}))
def test_required_functions_execute(
    config_path, prepared_configs, patch_usb_find, monkeypatch
):
    """Invoking each defined required quick method runs the parsed config script
    end to end, driving pin writes through the (mocked) hardware without error.

    Only functions the board actually defines are invoked, so configs that omit
    one (XFAIL_REQUIRED) still exercise whatever they do define.
    """
    config_dir, entries = prepared_configs
    entry = entries[os.path.basename(config_path)]

    board = load_board(config_path, config_dir, entries, patch_usb_find, monkeypatch)

    defined = [n for n in REQUIRED_FUNCTIONS if n in board.quick_methods]
    if not defined:
        # Omitting all three is a documented condition (XFAIL_REQUIRED); there is
        # nothing to execute here, so don't fail this test for it too.
        pytest.skip(f"{entry.name}: defines none of {REQUIRED_FUNCTIONS}")

    for name in defined:
        getattr(board, name)()


@pytest.mark.parametrize("config_path", config_params({**XFAIL_LOAD, **XFAIL_EXECUTE}))
def test_quick_method_call_wrapper(
    config_path, prepared_configs, patch_usb_find, monkeypatch
):
    """The QuickMethod wrapper used by the REST API invokes defined methods
    cleanly (returns {} on success, raises TACException on failure)."""
    config_dir, entries = prepared_configs

    board = load_board(config_path, config_dir, entries, patch_usb_find, monkeypatch)

    for name in REQUIRED_FUNCTIONS:
        if name in board.quick_methods:
            assert board.quick_methods[name].call() == {}


def test_ftdi_falls_back_to_default_config(
    prepared_configs, patch_usb_find, monkeypatch
):
    """An FTDI device whose product string matches no catalog entry falls back
    to the default.pinout.json config (the bundled FTDI Alpaca-Lite config,
    platform_id 13, that 'installconfigs' installs under that name)."""
    from conftest import FTDI_PRODUCT, FTDI_VENDOR, make_usb_device

    from pytactl import debugboard

    config_dir, _ = prepared_configs
    device = make_usb_device(
        FTDI_VENDOR, FTDI_PRODUCT, "UNKNOWN_SERIAL", product_str="No Such Descriptor"
    )
    patch_usb_find(device)

    board = debugboard.Board.create_board("UNKNOWN_SERIAL", config_dir)

    assert isinstance(board, debugboard.FtdiBoard)
    assert board.full_config["platform_id"] == 13


@requires_bundled_configs
def test_ftdi_falls_back_to_the_bundled_default(patch_usb_find):
    """The same fallback works against the config set shipped with the package,
    where that config keeps its own name instead of being copied to
    default.pinout.json."""
    from conftest import FTDI_PRODUCT, FTDI_VENDOR, make_usb_device

    import pytactl
    from pytactl import debugboard

    device = make_usb_device(
        FTDI_VENDOR, FTDI_PRODUCT, "UNKNOWN_SERIAL", product_str="No Such Descriptor"
    )
    patch_usb_find(device)

    board = debugboard.Board.create_board(
        "UNKNOWN_SERIAL", pytactl.PACKAGE_TAC_CONFIG_PATH
    )

    assert isinstance(board, debugboard.FtdiBoard)
    assert board.full_config["platform_id"] == 13
