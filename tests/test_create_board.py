# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for Board.create_board(): the USB vendor/product dispatch that maps a
plugged-in device onto the correct board implementation."""

import json
import shutil
import sys
import types
from unittest.mock import MagicMock

import pytest
from conftest import config_path_or_skip, make_usb_device

from pytactl import debugboard

Board = debugboard.Board

# Quick methods that the directly-driven (script-less) boards hard-code.
BUGHOPPER_METHODS = ("powerOn", "powerOff", "bootToEDL", "reset", "forceUsbcHostMode")


def test_create_board_returns_none_when_no_device(patch_usb_find, monkeypatch):
    monkeypatch.setattr(debugboard.usb.core, "find", lambda **kwargs: None)
    assert Board.create_board("missing", "./tac_configs") is None


def test_create_board_dispatches_ftdi(prepared_configs, patch_usb_find):
    config_dir, _ = prepared_configs
    device = make_usb_device(
        Board.ID_VENDOR_FTDI, Board.ID_PRODUCT_FTDI, "S1", product_str="anything"
    )
    patch_usb_find(device)
    board = Board.create_board("S1", config_dir)
    assert isinstance(board, debugboard.FtdiBoard)


def test_create_board_dispatches_psoc(prepared_configs, patch_usb_find, monkeypatch):
    config_dir, entries = prepared_configs
    # Pick any registered PSOC platform id; skip if no PSOC configs are present.
    psoc_ids = [e.match_value for e in entries.values() if e.platform_type == "PSOC"]
    if not psoc_ids:
        pytest.skip("no PSOC config files present to exercise the PSOC dispatch")

    device = make_usb_device(Board.ID_VENDOR_QCOM, Board.ID_PRODUCT_QCOM, "S2")
    patch_usb_find(device)
    monkeypatch.setattr(
        debugboard.PsocBoard,
        "_PsocBoard__get_board_id",
        lambda self: psoc_ids[0],
    )
    board = Board.create_board("S2", config_dir)
    assert isinstance(board, debugboard.PsocBoard)


def test_create_board_dispatches_bughopper_v1(patch_usb_find):
    device = make_usb_device(Board.ID_VENDOR_FTDI, Board.ID_PRODUCT_BUGHOPPER_V1, "S3")
    patch_usb_find(device)
    board = Board.create_board("S3", "./tac_configs")
    assert isinstance(board, debugboard.BughopperV1Board)
    for name in BUGHOPPER_METHODS:
        assert name in board.quick_methods


def test_create_board_bughopper_v2_exits_without_hid(patch_usb_find, monkeypatch):
    """If the optional hid module isn't loaded, BughopperV2 detection aborts."""
    device = make_usb_device(
        Board.ID_VENDOR_BUGHOPPER_V2, Board.ID_PRODUCT_BUGHOPPER_V2, "S4"
    )
    patch_usb_find(device)
    monkeypatch.delitem(sys.modules, "hid", raising=False)

    with pytest.raises(SystemExit):
        Board.create_board("S4", "./tac_configs")


def test_create_board_dispatches_bughopper_v2_with_hid(patch_usb_find, monkeypatch):
    """With a (fake) hid module present, BughopperV2 is constructed."""
    device = make_usb_device(
        Board.ID_VENDOR_BUGHOPPER_V2, Board.ID_PRODUCT_BUGHOPPER_V2, "S5"
    )
    patch_usb_find(device)

    hid_device = MagicMock(name="hid_device")
    hid_device.serial = "S5"
    fake_hid = types.ModuleType("hid")
    fake_hid.Device = MagicMock(return_value=hid_device)

    monkeypatch.setitem(sys.modules, "hid", fake_hid)
    monkeypatch.setattr(debugboard, "hid", fake_hid, raising=False)

    board = Board.create_board("S5", "./tac_configs")
    assert isinstance(board, debugboard.BughopperV2Board)
    for name in BUGHOPPER_METHODS:
        assert name in board.quick_methods


def test_bughopper_v2_hid_writes_prefix_report_id(patch_usb_find, monkeypatch):
    """Every HID write must begin with a 0x00 report-ID placeholder.

    hidapi consumes byte 0 of a write as the HID report ID. Without the 0x00
    prefix the command byte is swallowed as a (non-existent) report ID on
    Windows and the firmware never sees the GPIO command (the board silently
    fails to reboot). Assert the payload survives intact behind the prefix.
    """
    device = make_usb_device(
        Board.ID_VENDOR_BUGHOPPER_V2, Board.ID_PRODUCT_BUGHOPPER_V2, "S6"
    )
    patch_usb_find(device)

    hid_device = MagicMock(name="hid_device")
    hid_device.serial = "S6"
    fake_hid = types.ModuleType("hid")
    fake_hid.Device = MagicMock(return_value=hid_device)

    monkeypatch.setitem(sys.modules, "hid", fake_hid)
    monkeypatch.setattr(debugboard, "hid", fake_hid, raising=False)

    board = Board.create_board("S6", "./tac_configs")
    board.bootToEDL()

    writes = [call.args[0] for call in hid_device.write.call_args_list]
    assert writes, "bootToEDL wrote nothing"
    for payload in writes:
        assert payload[0] == 0x00, f"missing report-ID prefix: {list(payload)}"
        # byte 1 is the application command (CMD_GPIO), not stripped as report ID
        assert payload[1] == board.CMD_GPIO


# Required quick methods the PIC32CX config script defines and the test invokes.
PIC32CX_CONFIG = "TAC_PIC32CXAuto_54.pinout.json"
PIC32CX_METHODS = ("powerOn", "powerOff", "bootToEDL")


def _prepare_pic32cx_config_dir(tmp_path, usb_descriptor):
    """Copy the PIC32CX config into an isolated dir alongside a devicelist.json
    that maps ``usb_descriptor`` (the serial prefix before "XX") to it.

    Skips the calling test when the config is not available (it is part of the
    upstream config set, not of this repository).
    """
    shutil.copy(config_path_or_skip(PIC32CX_CONFIG), tmp_path / PIC32CX_CONFIG)
    catalog = {
        "catalog": [
            {
                "usb_descriptor": usb_descriptor,
                "configPath": PIC32CX_CONFIG,
            }
        ]
    }
    (tmp_path / "devicelist.json").write_text(json.dumps(catalog))
    return str(tmp_path)


def test_create_board_dispatches_pic32cx(tmp_path, patch_usb_find, monkeypatch):
    """A PIC32CX board is invisible to libusb, so usb.core.find returns nothing
    and create_board falls through to the udev-based Pic32cxBoard.detect path."""
    config_dir = _prepare_pic32cx_config_dir(tmp_path, "KARUSSELL")
    # usb.core.find finds no device; detect() (which would query udev) is forced.
    patch_usb_find(None)
    monkeypatch.setattr(
        debugboard.Pic32cxBoard, "detect", staticmethod(lambda serial: True)
    )

    # The platform is identified by the serial prefix before "XX".
    board = Board.create_board("KARUSSELLXX01", config_dir)

    assert isinstance(board, debugboard.Pic32cxBoard)
    assert board.full_config["platform_type"] == "PIC32CXAuto"


def test_create_board_pic32cx_required_methods(tmp_path, patch_usb_find, monkeypatch):
    """The PIC32CX board exposes powerOn/powerOff/bootToEDL as callable quick
    methods, and invoking them runs the parsed script through the mocked port."""
    config_dir = _prepare_pic32cx_config_dir(tmp_path, "KARUSSELL")
    patch_usb_find(None)
    monkeypatch.setattr(
        debugboard.Pic32cxBoard, "detect", staticmethod(lambda serial: True)
    )

    board = Board.create_board("KARUSSELLXX01", config_dir)

    for name in PIC32CX_METHODS:
        assert name in board.quick_methods
        assert callable(getattr(board, name, None))
        getattr(board, name)()


def test_create_board_pic32cx_no_matching_config_exits(
    tmp_path, patch_usb_find, monkeypatch
):
    """When no catalog entry matches the serial prefix, construction exits."""
    config_dir = _prepare_pic32cx_config_dir(tmp_path, "KARUSSELL")
    patch_usb_find(None)
    monkeypatch.setattr(
        debugboard.Pic32cxBoard, "detect", staticmethod(lambda serial: True)
    )

    with pytest.raises(SystemExit):
        Board.create_board("UNKNOWNXX01", config_dir)
