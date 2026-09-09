# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Shared fixtures and helpers for the pytactl test suite.

The tests mock the USB layer (and the underlying GPIO/serial hardware) so that
every ``*.pinout.json`` config of the installed config set can be loaded through
the exact same code path that ``pytactl.shell`` and ``pytactl.service`` use:
``Board.create_board()``.

The config set is the one ``pytactl installconfigs`` produces: every upstream
config converted to the shared pinout format (see :mod:`pytactl.tacconfig`).

A handful of configs are deliberately handled specially (see the maps below):

* EXCLUDED_CONFIGS   - not driven by the config-script path at all, so the suite
                       does not try to load them through FtdiBoard/PsocBoard.
* XFAIL_LOAD         - configs that fail to parse/exec. Upstream data problems
                       that cannot be fixed from the config alone; tracked as
                       expected failures.
* XFAIL_REQUIRED     - configs that load fine but legitimately omit one or more
                       of powerOn/powerOff/bootToEDL (the README notes that not
                       every board defines every command).
* XFAIL_EXECUTE      - configs whose scripts drive a command that no pin in the
                       config defines, so invoking the quick method raises.
"""

import glob
import json
import os
import shutil
from collections import namedtuple
from unittest.mock import MagicMock

import pytest

import pytactl
from pytactl import debugboard, tacconfig

# The config set is discovered by its shared pinout files; the UI overlays that
# may sit beside them upstream are not loaded by pytactl.
PINOUT_GLOB = "*" + tacconfig.PINOUT_EXTENSION


def resolve_config_dir():
    """Locate the directory holding the full TAC config set, or ``None``.

    Candidates, in order - the same precedence ``pytactl`` itself applies, plus
    the environment override on top:

    1. ``$PYTACTL_TAC_CONFIG_DIR`` - explicit override, for distro packagers and
       anyone who unpacks a config set somewhere of their own choosing.
    2. ``pytactl.INSTALLED_TAC_CONFIG_PATH`` - a newer upstream set pulled with
       "pytactl installconfigs", so the suite tests against that when it exists.
    3. ``pytactl.PACKAGE_TAC_CONFIG_PATH`` - the config set vendored in this
       repository, which is what a plain checkout tests against.

    Returns ``None`` when none of them holds a ``.pinout.json`` file - e.g. a
    distro build that strips the bundled configs - so that the config-driven
    tests skip instead of failing with FileNotFoundError.
    """
    for candidate in (
        os.environ.get("PYTACTL_TAC_CONFIG_DIR"),
        pytactl.INSTALLED_TAC_CONFIG_PATH,
        pytactl.PACKAGE_TAC_CONFIG_PATH,
    ):
        if candidate and glob.glob(os.path.join(candidate, PINOUT_GLOB)):
            return candidate
    return None


# Directory holding the config set under test, or None when it is unavailable.
CONFIG_DIR = resolve_config_dir()

NO_CONFIGS_REASON = (
    "TAC config set not available: the configs bundled in pytactl/tac_configs "
    "are missing, and neither PYTACTL_TAC_CONFIG_DIR nor the directory written "
    "by 'pytactl installconfigs' holds any .pinout.json file"
)

# Skip marker for tests that cannot run without a config set.
requires_configs = pytest.mark.skipif(CONFIG_DIR is None, reason=NO_CONFIGS_REASON)

# Some tests are about the config set vendored into the package specifically,
# not whichever set was resolved above. A distro that de-vendors those configs
# and repoints PYTACTL_TAC_CONFIG_DIR at its own copy has nothing for them to
# check, so they skip rather than fail the build.
BUNDLED_CONFIGS = bool(
    glob.glob(os.path.join(pytactl.PACKAGE_TAC_CONFIG_PATH, PINOUT_GLOB))
)

requires_bundled_configs = pytest.mark.skipif(
    not BUNDLED_CONFIGS,
    reason="the config set bundled in pytactl/tac_configs is not present",
)


def config_path_or_skip(name):
    """Return the path to config ``name``, skipping the test if it is absent."""
    if CONFIG_DIR is None:
        pytest.skip(NO_CONFIGS_REASON)
    path = os.path.join(CONFIG_DIR, name)
    if not os.path.isfile(path):
        pytest.skip(f"{name} not present in {CONFIG_DIR}")
    return path


# USB vendor/product pairs that Board.create_board() dispatches on.
FTDI_VENDOR = debugboard.Board.ID_VENDOR_FTDI  # 0x0403
FTDI_PRODUCT = debugboard.Board.ID_PRODUCT_FTDI  # 0x6011
QCOM_VENDOR = debugboard.Board.ID_VENDOR_QCOM  # 0x05C6
QCOM_PRODUCT = debugboard.Board.ID_PRODUCT_QCOM  # 0x9302

# Configs that are not loaded through the config-script path and so are not part
# of the data-driven config tests.
EXCLUDED_CONFIGS = {
    # PIC32CX uses a third dispatch path (udev detection + serial-prefix config
    # matching) rather than the FTDI/PSOC USB-descriptor path the data-driven
    # tests model. Covered directly by test_create_board_dispatches_pic32cx.
    "TAC_PIC32CXAuto_54.pinout.json": "PIC32CXAuto uses a dedicated dispatch path",
    # Bughopper board: handled by BughopperV1Board/BughopperV2Board (driven over
    # USB control / HID transfers), not by a config script.
    "TAC_FTDI_80.pinout.json": "Bughopper board, handled by a dedicated board class",
}

# Configs that fail to parse/exec. Upstream data problems: left unchanged here,
# because fixing one means supplying a value only the board's owner knows.
XFAIL_LOAD = {
    "TAC_FTDI_72.pinout.json": (
        "script uses $edl/$uefi/$fastboot but the config declares no variables; "
        "the delays are board timings only the config can supply"
    ),
}

# Configs that load but do not define all three of powerOn/powerOff/bootToEDL.
# Not defects to fix here: powerOn is not an alias for the powerOnTheDevice most
# of these do define - across the 28 configs defining both, powerOn calls
# powerOnTheDevice and then presses the power key for a board-specific hold time
# - so synthesising one would mean inventing a power-up sequence. The rest name
# their functions per board (two SoCs, two EDL entries) or have no script at all.
XFAIL_REQUIRED = {
    "TAC_FTDI_15.pinout.json": "defines bootToEDL only; no powerOn/powerOff",
    "TAC_FTDI_16.pinout.json": "no bootToEDL (board without EDL entry)",
    "TAC_FTDI_41.pinout.json": "uses spowerOn/bootToSDXEDL variants; no powerOn/bootToEDL",
    "TAC_FTDI_42.pinout.json": "empty script (SMART LABEL board defines no functions)",
    "TAC_FTDI_60.pinout.json": "defines bootToEDL/bootToUEFI only; no powerOn/powerOff",
    "TAC_PSOC_24.pinout.json": "defines bootToEDL variants only; no powerOn/powerOff",
    "TAC_PSOC_31.pinout.json": "defines bootToNADEDL/bootToEAPEDL variants; no bootToEDL",
}

# Configs whose powerOn/powerOff/bootToEDL drive a command that no pin in the
# config defines, so the bound quick method raises AttributeError when invoked.
# Each is an upstream data problem: a script carrying lines for hardware the
# board does not have (TAC_FTDI_29 is an RF switch box running a phone script;
# the M.2 modem cards have no power key or volume buttons). Which physical pin
# to bind - if any - is a question only the board's owner can answer, so these
# stay as they are. pytactl names the commands in a warning at load time.
XFAIL_EXECUTE = {
    "TAC_FTDI_23.pinout.json": "drives pkey/voldn/volup; M.2 card has no such pins",
    "TAC_FTDI_29.pinout.json": (
        "phone script on an RF switch box: drives battery/pedl/pkey/sedl/usb0/"
        "voldn/volup, board only has VC1-VC3"
    ),
    "TAC_FTDI_56.pinout.json": "drives usb1; board only defines usb0",
    "TAC_FTDI_65.pinout.json": "drives sedl/usb1; board defines neither",
    "TAC_FTDI_67.pinout.json": (
        "drives sedl/usb1, and sumxs2 which looks like a typo for the board's smuxs2"
    ),
    "TAC_FTDI_69.pinout.json": "drives pkey/voldn/volup; M.2 card has no such pins",
    "TAC_FTDI_72.pinout.json": "undeclared variables; never reaches execution",
    "TAC_FTDI_73.pinout.json": "drives sedl/usb1; board defines neither",
}


# Describes how to make Board.create_board() load a particular config file:
# which USB device to fake and which match key to advertise in devicelist.json.
#   platform_type: the value declared inside the pinout config (FTDI, PSOC, ...)
#   dispatch:      which board class create_board() routes to, "FTDI" or "PSOC".
#                  debugboard only knows two config-matching mechanisms:
#                  FtdiBoard matches by usb_descriptor, PsocBoard by platform_id.
ConfigEntry = namedtuple(
    "ConfigEntry", ["name", "path", "platform_type", "dispatch", "match_value"]
)


def discover_configs():
    """Return the testable ``.pinout.json`` config files (excluding special cases).

    Empty when the external config set is not available (see
    :func:`resolve_config_dir`).
    """
    if CONFIG_DIR is None:
        return []
    paths = sorted(glob.glob(os.path.join(CONFIG_DIR, PINOUT_GLOB)))
    return [p for p in paths if os.path.basename(p) not in EXCLUDED_CONFIGS]


def config_params(xfail_map=None):
    """Build pytest.param() entries for every testable config.

    Configs whose basename appears in ``xfail_map`` (basename -> reason) are
    marked xfail(strict=True), so a config that starts passing surfaces as an
    XPASS and prompts the entry to be removed.
    """
    xfail_map = xfail_map or {}
    configs = discover_configs()
    if not configs:
        # No config set available: emit a single skipped param so the test shows
        # up as skipped instead of pytest reporting an empty parameter set.
        return [pytest.param(None, id="no-configs", marks=requires_configs)]
    params = []
    for path in configs:
        base = os.path.basename(path)
        marks = []
        if base in xfail_map:
            marks.append(pytest.mark.xfail(reason=xfail_map[base], strict=True))
        params.append(pytest.param(path, id=base, marks=marks))
    return params


def make_usb_device(vendor, product_id, serial, product_str=""):
    """Build a fake pyusb device exposing the attributes debugboard reads."""
    dev = MagicMock(name="usb_device")
    dev.idVendor = vendor
    dev.idProduct = product_id
    dev.serial_number = serial
    dev.product = product_str
    return dev


@pytest.fixture
def patch_usb_find(monkeypatch):
    """Return a helper that makes ``usb.core.find`` return a given device."""

    def _install(device):
        monkeypatch.setattr(debugboard.usb.core, "find", lambda **kwargs: device)
        return device

    return _install


@pytest.fixture(autouse=True)
def mock_hardware(monkeypatch):
    """Replace every real hardware touch point with harmless fakes.

    - ``GpioAsyncController`` (FTDI GPIO) becomes a MagicMock, so FtdiPort/FtdiPin
      logic runs without a real FTDI chip.
    - ``PsocPort`` (which opens a serial port in __init__) is swapped for a fake.
    - ``sleep`` is neutralised so config scripts with ``delay`` run instantly.
    """
    monkeypatch.setattr(debugboard, "GpioAsyncController", MagicMock())
    monkeypatch.setattr(debugboard, "sleep", lambda *a, **k: None)

    class _FakePsocPort(debugboard.Port):
        def __init__(self, serialid):
            debugboard.Port.__init__(self, None, serialid)
            self.writes = []
            self.calls = []

        def write(self, value, pin=None):
            self.writes.append((value, pin))

        def call_method(self, method, value):
            self.calls.append((method, value))

        def close(self):
            pass

    monkeypatch.setattr(debugboard, "PsocPort", _FakePsocPort)

    class _FakePic32cxPort(debugboard.Port):
        """Stand-in for Pic32cxPort, which otherwise opens a real CDC serial
        port via udev in __init__."""

        def __init__(self, serialid):
            debugboard.Port.__init__(self, None, serialid)
            self.writes = []

        def write(self, value, pin=None):
            self.writes.append((value, pin))

        def close(self):
            pass

    monkeypatch.setattr(debugboard, "Pic32cxPort", _FakePic32cxPort)


@pytest.fixture(scope="session")
def prepared_configs(tmp_path_factory):
    """Build an isolated tac_config dir containing every testable config plus a
    generated ``devicelist.json`` that maps each one to a unique match key.

    The real ``devicelist.json`` matches most boards on a descriptor or platform
    id that only one config claims, so it cannot be used to reach every config
    from a fake USB device; this synthetic one can.

    Returns ``(config_dir, entries)`` where ``entries`` maps config basename to
    a :class:`ConfigEntry` describing how to load it via ``create_board``.

    Skips the requesting test when the external config set is unavailable.
    """
    if CONFIG_DIR is None:
        pytest.skip(NO_CONFIGS_REASON)

    dst = tmp_path_factory.mktemp("tac_configs")
    catalog = []
    entries = {}

    # Mirror what "installconfigs" does: install the bundled FTDI Alpaca-Lite
    # config as default.pinout.json, the file FtdiBoard falls back to when a
    # device's USB descriptor matches no catalog entry.
    shutil.copy(
        os.path.join(
            pytactl.PACKAGE_TAC_CONFIG_PATH, pytactl.BUNDLED_DEFAULT_CONFIG_FILENAME
        ),
        os.path.join(dst, pytactl.DEFAULT_CONFIG_FILENAME),
    )

    for path in discover_configs():
        base = os.path.basename(path)
        with open(path) as handle:
            cfg = json.load(handle)
        platform_type = cfg.get("platform_type")
        shutil.copy(path, os.path.join(dst, base))

        if platform_type == "PSOC":
            # PsocBoard matches catalog["platform_id"] against the board id read
            # over serial; assign a unique synthetic id we can return from the
            # mocked __get_board_id.
            platform_id = 90000 + len(entries)
            catalog.append({"platform_id": platform_id, "configPath": base})
            entries[base] = ConfigEntry(base, path, platform_type, "PSOC", platform_id)
        else:
            # FTDI (and any other FTDI-USB board): FtdiBoard matches
            # catalog["usb_descriptor"] against device.product. Use a unique
            # synthetic descriptor per file.
            descriptor = f"PYTACTL_TEST::{base}"
            catalog.append({"usb_descriptor": descriptor, "configPath": base})
            entries[base] = ConfigEntry(base, path, platform_type, "FTDI", descriptor)

    with open(os.path.join(dst, "devicelist.json"), "w") as handle:
        json.dump({"catalog": catalog}, handle)

    return str(dst), entries


def load_board(config_path, config_dir, entries, patch_usb_find, monkeypatch):
    """Load the board for ``config_path`` through Board.create_board, mocking the
    USB device so the config-by-USB-description dispatch selects this config."""
    entry = entries[os.path.basename(config_path)]

    if entry.dispatch == "FTDI":
        device = make_usb_device(
            FTDI_VENDOR, FTDI_PRODUCT, "FTDI_SERIAL", entry.match_value
        )
        patch_usb_find(device)
        return debugboard.Board.create_board("FTDI_SERIAL", config_dir)

    if entry.dispatch == "PSOC":
        device = make_usb_device(QCOM_VENDOR, QCOM_PRODUCT, "PSOC_SERIAL")
        patch_usb_find(device)
        # __get_board_id() talks to the board over a serial console; short-circuit
        # it to the synthetic platform id we registered in devicelist.json.
        monkeypatch.setattr(
            debugboard.PsocBoard,
            "_PsocBoard__get_board_id",
            lambda self: entry.match_value,
        )
        return debugboard.Board.create_board("PSOC_SERIAL", config_dir)

    raise AssertionError(f"unsupported dispatch {entry.dispatch!r}")
