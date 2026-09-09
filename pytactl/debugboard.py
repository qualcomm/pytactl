# Copyright (c)i 2025-2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

import json
import keyword
import logging
import os
import re
import sys
from time import sleep
from types import MethodType

import serial
import serial.tools.list_ports
import usb
from pexpect import fdpexpect
from pyftdi.gpio import GpioAsyncController

from . import default_config_file, tacconfig

logger = logging.getLogger()

try:
    import hid
except ImportError as e:
    # ignore importing hid. It's only required for bughopper v2.
    logger.error(e)
    pass

# wait time for other GPIOs to be truly set before touching the reset GPIO
PRE_RESET_DELAY = 0.1

BITMODE_CBUS = 0x20

# USB ctrl transfer request for FT230X CBUS
SIO_SET_BITMODE_REQUEST = 0x0B

# platform_type values in a TAC config that describe an FTDI-based board.
FTDI_PLATFORM_TYPES = ("FTDI", "FT232H")


class TACException(Exception):
    status_code = 420
    detail = "Unable to perform requested operation"


class ConfigScriptError(TACException):
    """A config script pytactl cannot turn into callable board commands."""

    detail = "Unable to parse the board configuration script"


class Board(dict):
    ID_VENDOR_FTDI = 0x0403
    ID_PRODUCT_FTDI = 0x6011
    ID_VENDOR_QCOM = 0x05C6
    ID_PRODUCT_QCOM = 0x9302
    ID_PRODUCT_BUGHOPPER_V1 = 0x6015
    ID_VENDOR_BUGHOPPER_V2 = 0x2341
    ID_PRODUCT_BUGHOPPER_V2 = 0xB001
    ID_VENDOR_PIC32CX = 0x04D8
    ID_PRODUCT_PIC32CX = 0x000A

    @classmethod
    def create_from_config(cls, config_file_path):
        return DummyBoard(config_file_path)

    @classmethod
    def known_boards(cls):
        # (idVendor, idProduct) -> human-readable board type. A pair may map
        # to more than one model (e.g. a pic32cx also enumerates as FTDI
        # 0403:6011); the label is the USB-level category, not the exact
        # model, which can only be resolved by querying the board.
        return {
            (cls.ID_VENDOR_FTDI, cls.ID_PRODUCT_BUGHOPPER_V1): "Bughopper V1",
            (cls.ID_VENDOR_FTDI, cls.ID_PRODUCT_FTDI): "FTDI",
            (cls.ID_VENDOR_QCOM, cls.ID_PRODUCT_QCOM): "PSOC",
            (cls.ID_VENDOR_BUGHOPPER_V2, cls.ID_PRODUCT_BUGHOPPER_V2): "Bughopper V2",
            (cls.ID_VENDOR_PIC32CX, cls.ID_PRODUCT_PIC32CX): "PIC32CX",
        }

    @staticmethod
    def _serial_ports():
        """Yield connected serial ports in a cross-platform way.

        Uses ``pyserial``'s ``list_ports`` instead of ``pyudev`` so the tool
        works on macOS and Windows as well as Linux (``pyudev`` depends on
        ``libudev``, which only exists on Linux).
        """
        yield from serial.tools.list_ports.comports()

    @classmethod
    def list_boards(cls):
        """Enumerate connected debug boards via their serial (tty) devices.

        Boards are discovered by walking the connected serial ports (the same
        place a board's serial port is found when driving it) and reading the
        ``serial_number`` / ``vid`` / ``pid`` reported by ``pyserial``. Using
        the serial port rather than the parent USB device ensures the serial
        number is available for all boards, including pic32cx.

        Returns a list of dicts with the board ``type``, USB ``serial``
        number and ``vid``/``pid``, de-duplicated by serial (a board may
        expose more than one serial interface).
        """
        known = cls.known_boards()
        boards = []
        seen = set()
        for port in cls._serial_ports():
            serial_short = port.serial_number
            if not serial_short or serial_short in seen:
                continue
            if port.vid is None or port.pid is None:
                continue
            vid = port.vid
            pid = port.pid
            board_type = known.get((vid, pid))
            if board_type is None:
                continue
            seen.add(serial_short)
            boards.append(
                {
                    "type": board_type,
                    "serial": serial_short,
                    "vid": vid,
                    "pid": pid,
                }
            )

        # The PIC32CX board also exposes an onboard FTDI FT4232H as a
        # UART/SPI bridge, which enumerates with the same VID/PID as a
        # standalone FTDI Alpaca board. It is not a separate debug board, so
        # drop it whenever a PIC32CX board is also present.
        if any(board["type"] == "PIC32CX" for board in boards):
            boards = [board for board in boards if board["type"] != "FTDI"]

        return boards

    @classmethod
    def create_board(cls, serial, tac_config_path):
        device = usb.core.find(serial_number=serial)
        if device:
            if (
                device.idVendor == Board.ID_VENDOR_FTDI
                and device.idProduct == Board.ID_PRODUCT_BUGHOPPER_V1
            ):
                logger.debug("Found Bughopper V1")
                return BughopperV1Board(device)
            if (
                device.idProduct == Board.ID_PRODUCT_FTDI
                and device.idVendor == Board.ID_VENDOR_FTDI
            ):
                logger.debug("Found FTDI Board")
                return FtdiBoard(device, tac_config_path)
            if (
                device.idProduct == Board.ID_PRODUCT_QCOM
                and device.idVendor == Board.ID_VENDOR_QCOM
            ):
                logger.debug("Found Psoc Board")
                return PsocBoard(device, tac_config_path)
            if (
                device.idVendor == Board.ID_VENDOR_BUGHOPPER_V2
                and device.idProduct == Board.ID_PRODUCT_BUGHOPPER_V2
            ):
                logger.debug("Found Bughopper V2")
                if "hid" not in sys.modules:
                    logger.error(
                        "hid module not loaded. Check if the required packages are installed"
                    )
                    sys.exit(1)

                return BughopperV2Board(device)

        # The PIC32CX board is driven purely over its CDC serial port and is not
        # visible to libusb (pyusb can't read its descriptors), so it is detected
        # via udev instead of usb.core.find.
        if Pic32cxBoard.detect(serial):
            logger.debug("Found PIC32CX Board")
            return Pic32cxBoard(serial, tac_config_path)

    def __init__(self):
        self.ports = {}
        self.pins = {}
        self.commands = {}
        self.quick_methods = {}
        self.usb_device = None
        dict.__init__(
            self, ports=self.ports, pins=self.pins, quick_methods=self.quick_methods
        )

    def logComment(self, comment):
        print(comment)

    def delay(self, length):
        d = float(length / 1000)
        self.logComment(f"Sleeping for {d} seconds")
        sleep(d)

    def create_pins(self):
        raise NotImplementedError()

    def create_ports(self):
        raise NotImplementedError()

    @staticmethod
    def _sanitize_name(name):
        # Turn an arbitrary command name into a valid Python identifier.
        sanitized = re.sub(r"\W", "_", name)
        if not sanitized or not (sanitized[0].isalpha() or sanitized[0] == "_"):
            # identifiers may not start with a digit (e.g. "12vpoweroff")
            sanitized = f"_{sanitized}"
        if keyword.iskeyword(sanitized):
            sanitized = f"{sanitized}_"
        return sanitized

    @staticmethod
    def _check_script_indentation(script):
        """Require every statement in the script to be indented with one tab.

        TAC config scripts are written with tabs: a ``def`` at column 0 and its
        body one tab in. pytactl holds configs to that rather than accepting
        whatever whitespace happens to be there, so that a stray space-indented
        line is reported against the config instead of being quietly absorbed.
        ``pytactl convertconfigs`` normalises a config directory to tabs, and
        the configs shipped with pytactl have been through it.
        """
        offenders = []
        for number, line in enumerate(script.splitlines(), 1):
            if not line.strip():
                # Blank lines are not statements, whatever they are made of.
                continue
            indent = line[: len(line) - len(line.lstrip())]
            if indent and indent != "\t":
                offenders.append(number)

        if offenders:
            raise ConfigScriptError(
                "Config script must indent statements with a single tab, but "
                "line(s) "
                + ", ".join(str(number) for number in offenders)
                + " use other whitespace. Run 'pytactl convertconfigs' on the "
                "config directory to normalise it."
            )

    def _check_script_commands(self, script):
        """Warn about commands the script drives that no pin implements.

        Such a command becomes an AttributeError deep inside the config script
        the first time the board is asked to do something - typically when
        someone tries to power a board on. Saying so up front, naming the
        commands, points at the config instead.
        """
        defined = set(re.findall(r"^def\s+(\w+)", script, re.MULTILINE))
        # Driving a pin is "<command> <value>"; a bare "<name>" line calls
        # another function. delay/logComment are the language's own statements.
        referenced = set(re.findall(r"^[ \t]+(\w+)[ \t]+\S", script, re.MULTILINE))
        unbound = sorted(
            referenced - defined - set(self.commands) - {"delay", "logComment"}
        )
        if unbound:
            logger.warning(
                "Config script drives %s, which no pin in this config defines; "
                "the functions using them will fail when called",
                ", ".join(repr(name) for name in unbound),
            )

    def parse_script(self):
        if self.full_config:
            initial_script = self.full_config["script"]
            new_script = initial_script

            self._check_script_indentation(initial_script)

            # Some configs use command names that aren't valid Python
            # identifiers (e.g. "12vpoweroff" starts with a digit). The script
            # is converted to Python and exec'd, and create_pins later does
            # setattr(self, pin.command, ...), so such names must be renamed
            # consistently in both the script and the pin command they map to.
            rename_map = {}
            for pin in self.full_config.get("pins", []):
                command = pin.get("command")
                if command and (
                    not command.isidentifier() or keyword.iskeyword(command)
                ):
                    if command not in rename_map:
                        rename_map[command] = self._sanitize_name(command)
                    pin["command"] = rename_map[command]

            for old_name, new_name in rename_map.items():
                logger.debug(f"Renaming command {old_name} to {new_name}")
                name_re = re.compile(rf"\b{re.escape(old_name)}\b")
                new_script = name_re.sub(new_name, new_script)

            # replace variables with actual values
            variables = self.full_config.get("variables", [])
            for variable in variables:
                # create global variables
                var_name = variable.get("name")
                if var_name:
                    var_re = re.compile(rf"\${var_name}")
                    new_script = var_re.sub(variable["default_value"], new_script)

            # A "$name" left over is a variable the script uses but the config
            # never declares, so there is no value to substitute. Say which,
            # rather than letting it reach exec() as a syntax error: the value
            # is a board timing that only the config can supply.
            undeclared = sorted(set(re.findall(r"\$(\w+)", new_script)))
            if undeclared:
                raise ConfigScriptError(
                    "Config script uses undeclared variable(s) "
                    + ", ".join(f"${name}" for name in undeclared)
                    + "; the config declares "
                    + (
                        ", ".join(sorted(v["name"] for v in variables if v.get("name")))
                        or "none"
                    )
                )

            # remove lines that start with // (// is a valid comment)
            new_script = "\n".join(
                line
                for line in new_script.splitlines()
                if not line.lstrip().startswith("//")
            )

            # remove commented lines
            fix_comments = re.compile(r"\/\/.*")
            new_script = fix_comments.sub("\r", new_script)
            fix_comments = re.compile(r"\/\/.*\r")
            new_script = fix_comments.sub("\r", new_script)

            # Keep the script in its own language, with comments and variables
            # already resolved, for the command check further down: everything
            # below rewrites it into Python.
            alpaca_script = new_script

            # fix function definitions
            fix_functions = re.compile(r"\(\)[\s]?", re.MULTILINE)
            new_script = fix_functions.sub("(self):\n", new_script)
            fix_functions2 = re.compile(r"\(\)[\s]?\r", re.MULTILINE)
            new_script = fix_functions2.sub("(self):\r", new_script)

            # add brackets to function calls
            fix_no_parenthesis = re.compile(
                r"([A-Za-z0-9_]+)\s([0-9]+)\s?", re.MULTILINE
            )
            new_script = fix_no_parenthesis.sub(r"self.\1(\2)\n", new_script)

            # fixes syntax of internal function calls
            fix_no_parenthesis_func = re.compile(r"\t([A-Za-z0-9_]+)$", re.MULTILINE)
            new_script = fix_no_parenthesis_func.sub(r"\tself.\1()", new_script)

            fix_no_parenthesis_empty = re.compile(
                r"self.([A-Za-z0-9_]+)\s?$", re.MULTILINE
            )
            new_script = fix_no_parenthesis_empty.sub(r"self.\1()", new_script)

            fix_log_comment = re.compile(
                r"logComment\s([a-zA-Z0-9=_\ ]+)\s?$", re.MULTILINE
            )
            new_script = fix_log_comment.sub(r'self.logComment("\1")', new_script)

            d = {}
            # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
            exec(new_script, d)

            # create ports
            self.create_ports()

            # create pins
            self.create_pins()

            self._check_script_commands(alpaca_script)

            # iterate keys explicitly: this is an exec'd namespace and the
            # `.keys()` form is load-bearing here (see config-parsing history)
            for name in d.keys():  # noqa: SIM118
                if not name.startswith("__"):
                    logger.debug(f"Adding {name}")
                    self.quick_methods.update({name: QuickMethod(self, name)})
                    method = MethodType(d.get(name), self)
                    setattr(self, name, method)


class DummyBoard(Board):
    def __init__(self, config_path):
        Board.__init__(self)
        self.config_path = config_path
        self.usb_device = lambda: None
        self.usb_device.serial_number = "123456"
        self.full_config = tacconfig.convert_file(self.config_path)
        self.parse_script()

    def create_ports(self):
        logger.debug("creating ports")
        # The pinout config names the platform explicitly, so the port layout
        # no longer has to be guessed from the file name.
        platform_type = self.full_config.get("platform_type", "")
        if platform_type in FTDI_PLATFORM_TYPES:
            for p in self.full_config.get("bus"):
                bus_name = p.get("bus")
                if p.get("bus_function") == 2:
                    self.ports.update({bus_name: DummyPort(bus_name, "123456")})

        if platform_type == "PSOC":
            self.ports.update({0: DummyPort("123456", None)})

    def create_pins(self):
        logger.debug("creating pins")
        for config in self.full_config.get("pins"):
            pin = DummyPin(self, config)
            pin.setPort(self.ports.get(0))
            logger.debug(f"Adding {pin.command}")
            self.pins.update({f"{pin.pin_number}": pin})
            self.commands.update({f"{pin.command}": f"{pin.command}"})
            setattr(self, pin.command, pin.set)


class BughopperV1Board(Board):
    def __init__(self, usb_device):
        Board.__init__(self)
        self.usb_device = usb_device
        self.quick_methods.update({"powerOn": QuickMethod(self, "powerOn")})
        self.quick_methods.update({"bootToEDL": QuickMethod(self, "bootToEDL")})
        self.quick_methods.update({"powerOff": QuickMethod(self, "powerOff")})
        self.quick_methods.update({"reset": QuickMethod(self, "reset")})
        self.quick_methods.update(
            {"forceUsbcHostMode": QuickMethod(self, "forceUsbcHostMode")}
        )

        self.EDL_BIT = 0b00000001
        self.POWER_DISABLE_BIT = 0b00000100
        self.VOL_DOWN_BIT = 0b00001000

        self.EDL_MASK = self.EDL_BIT << 4
        self.POWER_DISABLE_MASK = self.POWER_DISABLE_BIT << 4
        self.VOL_DOWN_MASK = self.VOL_DOWN_BIT << 4

    def _ftdi_set_bitmode(self, bitmask):
        bmRequestType = usb.util.build_request_type(
            usb.util.CTRL_OUT, usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE
        )

        wValue = bitmask | (BITMODE_CBUS << 8)
        self.usb_device.ctrl_transfer(bmRequestType, SIO_SET_BITMODE_REQUEST, wValue)

    def powerOn(self):
        logger.debug("Power cycle to normal boot mode")
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.POWER_DISABLE_BIT)
        sleep(PRE_RESET_DELAY)
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK | self.EDL_MASK | self.VOL_DOWN_MASK
        )

    def bootToEDL(self):
        logger.debug("Power cycle to USB boot mode")
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.POWER_DISABLE_BIT)
        sleep(PRE_RESET_DELAY)
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.EDL_MASK | self.EDL_BIT)

    def reset(self):
        logger.debug("MPU reset pulse")
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.POWER_DISABLE_BIT)
        sleep(PRE_RESET_DELAY)
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK | self.EDL_MASK | self.VOL_DOWN_MASK
        )

    def powerOff(self):
        logger.debug("MPU poweroff")
        self._ftdi_set_bitmode(self.POWER_DISABLE_MASK | self.POWER_DISABLE_BIT)

    def forceUsbcHostMode(self):
        logger.debug("Forcing host mode")
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK
            | self.VOL_DOWN_MASK
            | self.POWER_DISABLE_BIT
            | self.VOL_DOWN_BIT
        )
        sleep(PRE_RESET_DELAY)
        self._ftdi_set_bitmode(
            self.POWER_DISABLE_MASK
            | self.EDL_MASK
            | self.VOL_DOWN_MASK
            | self.VOL_DOWN_BIT
        )


class BughopperV2Board(Board):
    def __init__(self, usb_device):
        Board.__init__(self)
        self.usb_device = hid.Device(usb_device.idVendor, usb_device.idProduct)
        self.usb_device.serial_number = self.usb_device.serial
        self.quick_methods.update({"powerOn": QuickMethod(self, "powerOn")})
        self.quick_methods.update({"bootToEDL": QuickMethod(self, "bootToEDL")})
        self.quick_methods.update({"powerOff": QuickMethod(self, "powerOff")})
        self.quick_methods.update({"reset": QuickMethod(self, "reset")})
        self.quick_methods.update(
            {"forceUsbcHostMode": QuickMethod(self, "forceUsbcHostMode")}
        )

        self.CMD_GPIO = 0x1

        self.EDL_BIT = 0x1
        self.POWER_DISABLE_BIT = 0x4
        self.VOL_DOWN_BIT = 0x8

    def _hid_set_bitmode(self, command, gpio_value, mask=0xF):
        # The leading 0x00 is the HID report-ID placeholder for this unnumbered
        # RawHID report. hidapi always consumes byte 0 of a write as the report
        # ID: Linux/hidraw tolerates its absence, but on Windows the command
        # byte would be swallowed as a (non-existent) report ID and stripped,
        # so the firmware never sees the GPIO command. Prepending 0x00 keeps the
        # [command, gpio_value, mask] payload intact on every platform.
        self.usb_device.write(bytes([0x00, command, gpio_value, mask]))

    def powerOn(self):
        logger.debug("Power cycle to normal boot mode")
        self._hid_set_bitmode(
            self.CMD_GPIO, self.POWER_DISABLE_BIT, self.POWER_DISABLE_BIT
        )
        sleep(PRE_RESET_DELAY)
        self._hid_set_bitmode(
            self.CMD_GPIO,
            0x0,
            self.POWER_DISABLE_BIT | self.EDL_BIT | self.VOL_DOWN_BIT,
        )

    def bootToEDL(self):
        logger.debug("Power cycle to USB boot mode")
        self._hid_set_bitmode(
            self.CMD_GPIO, self.POWER_DISABLE_BIT, self.POWER_DISABLE_BIT
        )
        sleep(PRE_RESET_DELAY)
        self._hid_set_bitmode(
            self.CMD_GPIO, self.EDL_BIT, self.EDL_BIT | self.POWER_DISABLE_BIT
        )

    def reset(self):
        logger.debug("MPU reset pulse")
        self._hid_set_bitmode(
            self.CMD_GPIO, self.POWER_DISABLE_BIT, self.POWER_DISABLE_BIT
        )
        sleep(PRE_RESET_DELAY)
        self._hid_set_bitmode(
            self.CMD_GPIO,
            0x0,
            self.POWER_DISABLE_BIT | self.EDL_BIT | self.VOL_DOWN_BIT,
        )

    def powerOff(self):
        logger.debug("MPU poweroff")
        self._hid_set_bitmode(
            self.CMD_GPIO, self.POWER_DISABLE_BIT, self.POWER_DISABLE_BIT
        )

    def forceUsbcHostMode(self):
        logger.debug("Forcing host mode")
        self._hid_set_bitmode(
            self.CMD_GPIO,
            self.POWER_DISABLE_BIT | self.VOL_DOWN_BIT,
            self.POWER_DISABLE_BIT | self.VOL_DOWN_BIT,
        )
        sleep(PRE_RESET_DELAY)
        self._hid_set_bitmode(
            self.CMD_GPIO,
            self.VOL_DOWN_BIT,
            self.POWER_DISABLE_BIT | self.VOL_DOWN_BIT | self.EDL_BIT,
        )


class FtdiBoard(Board):
    def __init__(self, usb_device, tac_config_path):
        Board.__init__(self)
        self.usb_device = usb_device
        # Default config for FTDI Alpaca-Lite, used when the board's USB
        # descriptor matches no catalog entry.
        conf = default_config_file(tac_config_path)
        f = open(os.path.join(tac_config_path, "devicelist.json"))
        device_list = json.loads(f.read())
        f.close()
        catalog = device_list.get("catalog")
        conf_dict = next(
            (x for x in catalog if x.get("usb_descriptor") == self.usb_device.product),
            None,
        )
        if conf_dict:
            conf = os.path.join(
                tac_config_path, os.path.basename(conf_dict.get("configPath"))
            )

        if conf is None:
            logger.error("No matching FTDI config found")
            sys.exit(1)

        self.full_config = tacconfig.convert_file(conf)
        self.parse_script()

    def create_ports(self):
        for p in self.full_config.get("bus"):
            bus_name = p.get("bus")
            if p.get("bus_function") == 2:
                self.ports.update(
                    {bus_name: FtdiPort(bus_name, self.usb_device.serial_number)}
                )

    def create_pins(self):
        for p in self.full_config.get("pins"):
            pin = FtdiPin(self, p)
            pin.setPort(self.ports.get(pin.bus))
            logger.debug(f"Adding {pin.command}")
            self.pins.update({f"{pin.bus}{pin.pin_number}": pin})
            self.commands.update({f"{pin.command}": f"{pin.command}"})
            setattr(self, pin.command, pin.set)


class PsocBoard(Board):
    def __init__(self, usb_device, tac_config_path):
        Board.__init__(self)
        self.usb_device = usb_device
        conf = None
        f = open(os.path.join(tac_config_path, "devicelist.json"))
        device_list = json.loads(f.read())
        f.close()
        catalog = device_list.get("catalog")
        self.board_id = self.__get_board_id()
        conf_dict = next(
            (x for x in catalog if x.get("platform_id") == self.board_id), None
        )
        if conf_dict:
            conf = os.path.join(
                tac_config_path, os.path.basename(conf_dict.get("configPath"))
            )

        if conf is None:
            logger.error("No matching PSOC config found")
            sys.exit(1)

        self.full_config = tacconfig.convert_file(conf)
        self.parse_script()
        self.quick_methods.update({"devicePowerOn": QuickMethod(self, "devicePowerOn")})
        self.quick_methods.update(
            {"devicePowerOff": QuickMethod(self, "devicePowerOff")}
        )
        self.quick_methods.update(
            {"usbDevicePowerOn": QuickMethod(self, "usbDevicePowerOn")}
        )
        self.quick_methods.update(
            {"usbDevicePowerOff": QuickMethod(self, "usbDevicePowerOff")}
        )

    def devicePower(self, value):
        logger.debug(f"Calling devicePower {value}")
        if value:
            self.ports[0].call_method("devicePower", "on")
        else:
            self.ports[0].call_method("devicePower", "off")

    def usbDevicePower(self, value):
        logger.debug(f"Calling usbDevicePower {value}")
        if value:
            self.ports[0].call_method("usbDevicePower", "on")
        else:
            self.ports[0].call_method("usbDevicePower", "off")

    def devicePowerOn(self):
        self.devicePower(True)

    def devicePowerOff(self):
        self.devicePower(False)

    def usbDevicePowerOn(self):
        self.usbDevicePower(True)

    def usbDevicePowerOff(self):
        self.usbDevicePower(False)

    def __get_board_id(self):
        # connect to serial and call "getboardid"
        serial_port = None
        for port in serial.tools.list_ports.comports():
            if port.serial_number == self.usb_device.serial_number:
                serial_port = port.device
                break

        logger.info(f"Opening {serial_port}")
        expect_connection = None

        connection = serial.Serial()
        connection.baudrate = 115200
        connection.port = serial_port
        try:
            connection.open()
        except serial.SerialException as e:
            logger.error("Serial Exception")
            logger.error(e)
            sys.exit(1)
        expect_connection = fdpexpect.fdspawn(connection)
        if not expect_connection.isalive():
            logger.error("Expect connection not created")
            sys.exit(1)

        expect_connection.send("\r")
        expect_connection.expect("CMD")
        expect_connection.send("getboardid\r")
        expect_connection.expect("ok")
        ret_value = expect_connection.before
        expect_connection.close()
        # find returned value
        r = re.search(r"\d+", ret_value.decode())
        if r:
            return int(r.group(0))
        return None

    def create_ports(self):
        self.ports.update({0: PsocPort(self.usb_device.serial_number)})

    def create_pins(self):
        for config in self.full_config.get("pins"):
            pin = PsocPin(self, config)
            pin.setPort(self.ports.get(0))
            logger.debug(f"Adding {pin.command}")
            self.pins.update({f"{pin.pin_number}": pin})
            self.commands.update({f"{pin.command}": f"{pin.command}"})
            setattr(self, pin.command, pin.set)


class Pic32cxBoard(Board):
    @staticmethod
    def detect(serial_number):
        """Return True if a PIC32CX tty matching ``serial_number`` is present.

        The PIC32CX board is not visible to libusb, so it is discovered via
        the serial port by matching the serial number together with its USB
        VID/PID.
        """
        for port in serial.tools.list_ports.comports():
            if port.serial_number != serial_number:
                continue
            if port.vid is None or port.pid is None:
                continue
            if (
                port.vid == Board.ID_VENDOR_PIC32CX
                and port.pid == Board.ID_PRODUCT_PIC32CX
            ):
                return True
        return False

    def __init__(self, serial, tac_config_path):
        Board.__init__(self)
        self.usb_device = lambda: None
        self.usb_device.serial_number = serial
        conf = None
        f = open(os.path.join(tac_config_path, "devicelist.json"))
        device_list = json.loads(f.read())
        f.close()
        catalog = device_list.get("catalog")
        # The platform is identified by the part of the serial number before
        # "XX", which matches the usb_descriptor field in devicelist.json.
        self.usb_descriptor = serial.split("XX")[0]
        conf_dict = next(
            (x for x in catalog if x.get("usb_descriptor") == self.usb_descriptor),
            None,
        )
        if conf_dict:
            conf = os.path.join(
                tac_config_path, os.path.basename(conf_dict.get("configPath"))
            )

        if conf is None:
            logger.error(
                "No matching PIC32CX config found for usb_descriptor %r "
                "(serial %s). Known usb_descriptors: %s",
                self.usb_descriptor,
                serial,
                ", ".join(
                    sorted(
                        x.get("usb_descriptor")
                        for x in catalog
                        if x.get("usb_descriptor")
                    )
                ),
            )
            sys.exit(1)

        self.full_config = tacconfig.convert_file(conf)
        self.parse_script()

    def create_ports(self):
        self.ports.update({0: Pic32cxPort(self.usb_device.serial_number)})

    def create_pins(self):
        for config in self.full_config.get("pins"):
            pin = Pic32cxPin(self, config)
            pin.setPort(self.ports.get(0))
            logger.debug(f"Adding {pin.command}")
            self.pins.update({f"{pin.pin_number}": pin})
            self.commands.update({f"{pin.command}": f"{pin.command}"})
            setattr(self, pin.command, pin.set)


class QuickMethod(dict):
    def __init__(self, board, name):
        self.name = name
        self.board = board
        dict.__init__(self, name=self.name, board=self.board.usb_device.serial_number)

    def call(self):
        try:
            getattr(self.board, self.name)()
        except Exception as e:
            logger.error(e)
            raise TACException
        return {}


class Pin(dict):
    def __init__(self, board, config):
        self.board = board
        self.bus = config.get("bus")
        self.command = config.get("command")
        self.initial_value = config.get("initial_value")
        self.value = self.initial_value
        self.input = config.get("input")
        self.inverted = config.get("inverted")
        self.pin_number = int(config.get("pin_number"))
        self.help_hint = config.get("help_hint")
        self.port = None
        dict.__init__(
            self,
            bus=self.bus,
            command=self.command,
            initial_value=self.initial_value,
            value=self.value,
            input=self.input,
            inverted=self.inverted,
            pin_number=self.pin_number,
            help_hint=self.help_hint,
            port=self.port,
        )

    def set(self, value):
        self.value = value
        super().__setitem__("value", self.value)

    def initialize(self):
        raise NotImplementedError()

    def setPort(self, port):
        self.port = port
        super().__setitem__("port", self.port)


class DummyPin(Pin):
    def initialize(self):
        logger.info(f"Initializing pin: {self.bus} {self.pin_number}")


class FtdiPin(Pin):
    def __init__(self, board, config):
        Pin.__init__(self, board, config)
        self.pin_number_mask = 1 << self.pin_number

    def set(self, value):
        super().set(value)
        if not self.port:
            logger.warning(f"No port set for pin {self.bus}{self.pin_number}")
            return
        logger.debug(f"Port {self.bus} status: 0x{self.port.status:02X}")
        logger.debug(f"Setting {self.bus}{self.pin_number} to {value}")
        logger.debug(f"Mask: 0x{self.pin_number_mask:02X}")
        if self.value:
            self.port.write(self.port.status | self.pin_number_mask)
        else:
            self.port.write(self.port.status & ~self.pin_number_mask)

    def initialize(self):
        self.set(int(self.initial_value))

    def setPort(self, port):
        super().setPort(port)
        self.initialize()


class PsocPin(Pin):
    def __init__(self, board, config):
        Pin.__init__(self, board, config)

    def set(self, value):
        lcl_value = None
        try:
            lcl_value = int(value)
        except ValueError:
            logger.error("Value has to be int")
            logger.error(f"Received {value}")
            return
        super().set(lcl_value)
        if not self.port:
            logger.warning(f"No port set for pin {self.pin_number}")
            return
        logger.debug(f"Setting {self.pin_number} to {lcl_value}")
        self.port.write(lcl_value, self.pin_number)

    def initialize(self):
        self.set(int(self.initial_value))

    def setPort(self, port):
        super().setPort(port)
        self.initialize()


class Pic32cxPin(Pin):
    def __init__(self, board, config):
        Pin.__init__(self, board, config)

    def set(self, value):
        lcl_value = None
        try:
            lcl_value = int(value)
        except ValueError:
            logger.error("Value has to be int")
            logger.error(f"Received {value}")
            return
        super().set(lcl_value)
        if not self.port:
            logger.warning(f"No port set for pin {self.pin_number}")
            return
        logger.debug(f"Setting {self.pin_number} to {lcl_value}")
        self.port.write(lcl_value, self.pin_number)

    def initialize(self):
        # PIC32CX configs define no initial pin value, and the reference TAC
        # tool does not drive any pins on connect. Leave pins untouched until
        # explicitly commanded so connecting does not actuate the board.
        pass

    def setPort(self, port):
        super().setPort(port)


class Port(dict):
    def __init__(self, bus, serial):
        self.serial = serial
        self.bus = bus
        dict.__init__(self, serial=self.serial, bus=self.bus)

    def write(self, value, pin=None):
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()


class DummyPort(Port):
    def write(self, value, pin=None):
        logger.info(f"Writing to port {self.bus} {self.serial}")

    def close(self):
        logger.info(f"Closing port {self.bus}")


class PsocPort(Port):
    def __init__(self, serialid):
        Port.__init__(self, None, serialid)
        self.serial_port = None
        for port in serial.tools.list_ports.comports():
            if port.serial_number == serialid:
                self.serial_port = port.device
                break

        self.expect_connection = None

        self.connection = serial.Serial()
        self.connection.baudrate = 115200
        self.connection.port = self.serial_port
        try:
            self.connection.open()
        except serial.SerialException as e:
            logger.error("Serial Exception")
            logger.error(e)
            sys.exit(1)
        self.expect_connection = fdpexpect.fdspawn(self.connection)
        if not self.expect_connection.isalive():
            logger.error("Expect connection not created")
            sys.exit(1)

    def close(self):
        if self.connection and self.connection.is_open:
            self.connection.close()

    def call_method(self, method, value):
        logger.debug(f"Calling {method} {value}")
        if self.expect_connection and self.expect_connection.isalive():
            self.expect_connection.send("\r")
            self.expect_connection.expect("CMD")
            message = f"{method} {value}\r"

            self.expect_connection.send(message)
            self.expect_connection.expect("ok")
        else:
            logger.error("No expect connection")
            sys.exit(1)

    def write(self, value, pin=None):
        if not pin:
            logger.warning("No pin selected")
            return
        if self.expect_connection and self.expect_connection.isalive():
            self.expect_connection.send("\r")
            self.expect_connection.expect("CMD")
            message = f"pin {value} {pin}\r"

            self.expect_connection.send(message)
            self.expect_connection.expect("ok")
        else:
            logger.error("No expect connection")
            sys.exit(1)


class Pic32cxPort(Port):
    def __init__(self, serialid):
        Port.__init__(self, None, serialid)
        self.serial_port = None
        for port in serial.tools.list_ports.comports():
            if port.serial_number == serialid:
                self.serial_port = port.device
                break

        if self.serial_port is None:
            logger.error(f"No serial port found for {serialid}")
            sys.exit(1)

        self.connection = serial.Serial()
        self.connection.baudrate = 115200
        self.connection.timeout = 0.5
        self.connection.port = self.serial_port
        try:
            self.connection.open()
        except serial.SerialException as e:
            logger.error("Serial Exception")
            logger.error(e)
            sys.exit(1)

        # Clear any pending data in the firmware's command buffer.
        self.__send("echo 1")

    def __send(self, message):
        logger.debug(f"Sending: {message}")
        self.connection.reset_input_buffer()
        self.connection.write(f"{message}\n".encode())
        self.connection.flush()
        # Drain the firmware's reply so it doesn't leak into the next command.
        response = self.connection.read_until().decode(errors="replace")
        logger.debug(f"Response: {response!r}")

    def write(self, value, pin=None):
        if pin is None:
            logger.warning("No pin selected")
            return
        # The firmware expects 'port 0' pins to be zero-padded to three digits.
        pin_str = str(pin)
        if len(pin_str) < 3:
            pin_str = "0" + pin_str
        self.__send(f"CONF:DIG:ON {int(bool(value))} (@{pin_str})")

    def close(self):
        if self.connection and self.connection.is_open:
            self.connection.close()


class FtdiPort(Port):
    def __init__(self, bus, serial, direction=0xFF):
        Port.__init__(self, bus, serial)
        self.direction = direction
        self.port = ord(self.bus) - ord("A") + 1
        self.url = f"ftdi://::{self.serial}/{self.port}"
        self.pins = {}
        self.status = 0x0
        self.gpio = GpioAsyncController()
        self.gpio.configure(self.url, direction=self.direction)

    def write(self, value, pin=None):
        self.status = value
        logger.debug(f"Port {self.bus} value: 0x{self.status:02X}")
        self.gpio.write(self.status)

    def close(self):
        logger.debug(f"Closing port {self.bus}")
        self.gpio.close()
