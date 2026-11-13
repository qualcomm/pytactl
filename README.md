# pytactl

Python implementation of Test Automation Controller (TAC/Alpaca) for controlling Qualcomm debug boards.
It uses config files and PSOC firmware from the original TAC (Alpaca) system.

# Installation

Install the `pytactl` command on your system from [PyPI](https://pypi.org/project/pytactl/)
with [pipx](https://pipx.pypa.io):

    pipx install pytactl

To install from a checkout of this repository instead:

    pipx install .

This puts a single `pytactl` entry point on your `PATH`. Note that the `hid`
dependency needs `libhidapi` package to be installed (required for Bughopper V2 boards).

Alternatively, for development in a virtualenv:

    virtualenv -p python3 venv
    . ./venv/bin/activate
    pip install -e .

Run `pytactl -h` to see all available options.

## System dependencies

pytactl uses `hidapi` for HID-based debug boards, including Bughopper V2.
Install the native HIDAPI library before installing or running `pytactl`.

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y libhidapi-hidraw0 libhidapi-libusb0
```

### Fedora

```bash
sudo dnf install -y hidapi
```

## USB permissions

By default, USB devices are not accessible without root. Install the udev rules
file to have access to the supported boards as an user:

    sudo cp 60-pytactl.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules && sudo udevadm trigger

Then make sure your user is in the `plugdev` group (log out and back in after):

    sudo usermod -aG plugdev $USER

# Configuration

**Bughopper boards (V1 and V2) work out of the box.** They are self-describing and need no
config files.

**All other debug boards (FTDI, PSOC, PIC32CX) are driven from a configuration file, and
the configs ship with pytactl.** They live in `pytactl/tac_configs/`, vendored from
[qcom-test-automation-controller](https://github.com/qualcomm/qcom-test-automation-controller/tree/main/configurations)
and converted to the pinout format pytactl loads (see
[Config file format](#config-file-format) below). Nothing needs fetching before a board can be
driven:

    pytactl shell --serial <serial>

`pytactl/tac_configs/README.md` records the upstream commit the set was taken from.

## Listing the configs pytactl finds

`listconfigs` shows which config directory pytactl uses (the installed set, the bundled
one, or the one given with `--tac-config-path`), each debug board `devicelist.json`
lists with its config file, platform id and USB descriptor, and any config file no
board entry refers to:

    pytactl listconfigs --log-level INFO

Every config a board entry refers to is loaded, and a missing or unreadable one is
flagged. The command exits with status 1 when `devicelist.json` cannot be read, lists
no boards, or refers to a missing or invalid config, so it can serve as a check that
the configs are installed (for example in a distribution's package tests). Config
scripts are not parsed; a script error is reported when a board is driven with that
config.

## Using a different config set

`--tac-config-path <dir>` points pytactl at another directory of configs; without it, the
bundled set is used.

To use configs that are not in the bundled set — a newer checkout of
qcom-test-automation-controller, configs for boards of your own, or a config edited in QTAC —
convert their `configurations/` directory and point pytactl at the result:

    pytactl convertconfigs /path/to/qcom-test-automation-controller/configurations \
      --output /path/to/install
    pytactl shell --tac-config-path /path/to/install --serial <serial>

Without `--output` the configs are converted in place, beside the originals. `--dry-run` reports
what would be written without writing it, and `--write-overlay` also writes the UI half of each
config (see below), producing the complete two-file layout rather than only the half pytactl uses.

Note: some configs in qcom-test-automation-controller currently have syntax issues, and the
bundled set carries fixes upstream does not have (see
`scripts/known-config-differences.json`); pick the ones that match your board.

### `installconfigs` (deprecated)

`pytactl installconfigs` downloads every config file and `devicelist.json` from the config
repository, converts them, and installs the result into a per-user data directory (resolved with
[platformdirs](https://pypi.org/project/platformdirs/), e.g. `~/.local/share/pytactl` on Linux).
It is deprecated and will be removed in a future release:

- the installed configs are raw upstream, without the fixes the bundled set carries;
- once that directory is populated, every command not given `--tac-config-path` uses it
  instead of the bundled set, and keeps doing so after pytactl is upgraded.

pytactl logs a warning whenever it falls back to the installed set. Delete the directory to go
back to the bundled configs, and use `convertconfigs` with `--tac-config-path` for a config set
of your own. `pytactl listconfigs` shows which directory is in use.

## Config file format

Upstream stores one config file per debug board. Historically that was a single combined `.tcnf`
file holding the hardware pinout, the automation script *and* the Qt UI layout (tabs, buttons,
labels, tooltips, grid cells).
[PR #54](https://github.com/qualcomm/qcom-test-automation-controller/pull/54) splits that into
two sibling files, so the hardware description can be reused outside the QTAC application:

| File | Contents |
|------|----------|
| `TAC_<CHIP>_<ID>.pinout.json` | Pins, bus map, script variables and the Alpaca script. Self-describing: `"format": "tac-pinout"`. |
| `TAC_<CHIP>_<ID>.tcnf` | UI layout only, linked to its pinout file through `pinout_ref`. |

pytactl drives hardware and has no UI, so it loads the `.pinout.json` half and ignores the
overlay. `pytactl.tacconfig` implements the same split (it reproduces upstream's own output for
every config in that PR), which is what `installconfigs` and `convertconfigs` apply, so pytactl
does not have to wait for the split to land upstream and will consume upstream's `.pinout.json`
files unchanged once it does.

All three shapes are accepted wherever a single config file is given with `--config-file-path`: a
`.pinout.json`, a split `.tcnf` (its `pinout_ref` sibling is loaded from the same directory), or a
legacy combined `.tcnf`, which is converted in memory.

One thing does not survive the split unaided: `enabled` is a UI field, but several configs wire
the same command name to two physical pins and disable one of them. The conversion therefore
drops a disabled pin when an enabled pin claims the same command, so the command keeps driving the
line it drove before.

### Script indentation

A config's `script` is written in the "Alpaca" automation language: a `def` at column 0
and its body **indented with a single tab**, with no nested blocks.

    def powerOn()
    →   battery 1
    →   delay 800
    →   pkey 0

pytactl requires that tab. A statement indented with spaces — or with more than one tab —
is rejected at load time with a `ConfigScriptError` naming the offending lines, rather
than being quietly accepted: indentation is part of the config file format, and a config
that does not follow it is a config to fix, not to work around.

`installconfigs` and `convertconfigs` re-indent scripts with tabs as they import them and
log each file they touch, so configs that come through either command satisfy the rule and
the set bundled with pytactl already does. Three upstream configs (`TAC_FTDI_51`,
`TAC_FTDI_52`, `TAC_FTDI_77`) indent two lines each with spaces and are corrected on the
way in.

This means a **raw** upstream `.tcnf` handed straight to `--config-file-path` may be
rejected where the converted config loads. That is deliberate — convert the directory
first:

    pytactl convertconfigs /path/to/qcom-test-automation-controller/configurations \
      --output /path/to/install

If you are upgrading and a board that used to work now reports a `ConfigScriptError`, your
installed config directory predates this rule: delete it to use the bundled configs, or run
`pytactl convertconfigs <dir>` to normalise it in place.

### Provenance annotation

`installconfigs` and `convertconfigs` stamp each config they write with where it came from, in a
`source` object below the format envelope — the repository, the commit (a ref like `main` moves,
so the SHA it resolved to at import time is what gets recorded), the directory within the
repository, and the upstream file name:

    {
      "$schema": "https://qualcomm.github.io/tac/schemas/pinout-1.0.json",
      "format": "tac-pinout",
      "schema_version": "1.0",
      "source": {
        "repository": "https://github.com/qualcomm/qcom-test-automation-controller.git",
        "commit": "757cc972c88e4a1098881b2bc73f3d53eac286be",
        "path": "configurations",
        "file": "TAC_FTDI_15.tcnf"
      },
      ...
    }

`convertconfigs` reads this from the git checkout it is pointed at, and leaves an already
annotated config alone, so re-converting an imported set in place does not restamp it with the
wrong repository.

This is pytactl's one addition to the format: upstream's `schemas/pinout-1.0.json` sets
`"additionalProperties": false`, so an annotated config does not validate against it unchanged.
Pass `--no-annotate` to either subcommand for configs that must.

`devicelist.json` maps board hardware IDs to their config files, and must be present in
the `--tac-config-path` directory for FTDI/PSOC boards. `installconfigs` and `convertconfigs`
rewrite its `configPath` entries to the converted file names. Example entry for a PSOC board:

    {
      "catalog": [
        {
          "platform_id": 17,
          "configPath": "TAC_PSOC_17.pinout.json"
        }
      ]
    }

## Finding your board's serial number

The `--serial` argument takes the USB serial number, not a device path. The easiest way to
discover connected boards and their serial numbers is the `list` subcommand:

    pytactl list

It prints every recognised debug board with its type, USB vendor/product ID, and serial number
(read from udev, the same `ID_SERIAL_SHORT` value you pass to `--serial`):

    Connected debug boards:
      Bughopper V1   vid:pid=0403:6015  serial=DP05DIAN
      PSOC           vid:pid=05c6:9302  serial=0123456789

Alternatively, find it manually with `udevadm`:

    udevadm info /dev/ttyACM0 | grep ID_SERIAL_SHORT

Or using `lsusb` (replace `VID:PID` with `0403:6011` for FTDI or `05c6:9302` for PSOC):

    lsusb -v -d VID:PID | grep iSerial

When exactly one debug board is connected, `shell`, `oneshot` and `listcommands` use it and
`--serial` can be left out. With no board or several connected they exit with an error,
listing the serial numbers of the connected boards to choose from. Given several serials, they use the first.

# Using as a shell

Start the interactive shell with the `shell` subcommand:

    pytactl shell --serial <ID_SERIAL_SHORT>

Optional arguments:

    --tac-config-path <dir>   # use a config directory other than the bundled one (see Configuration)
    --log-level DEBUG         # log verbosity (default: DEBUG)

Once started, the shell prompt accepts commands generated from your board's config script. The available commands depend on the config — not all boards define every command (e.g. newer configs may omit `powerOn`/`powerOff`). Typical commands:

**Power control:**

    powerOn
    powerOff
    devicePowerOn
    devicePowerOff
    usbDevicePowerOn
    usbDevicePowerOff

**Boot modes:**

    bootToEDL
    bootToFastboot
    bootToUEFI
    reset

**GPIO pins** (use with `1` to assert, `0` to deassert):

    pkey 1      # press power key
    pkey 0      # release power key
    volup 1
    voldn 1

Type `help` in the shell to list all commands available for your specific board.

# Running a single command

Use the `oneshot` subcommand to run one command and exit, without entering the interactive
shell. This is handy for scripting:

    pytactl oneshot bootToEDL --serial <ID_SERIAL_SHORT>
    pytactl oneshot reset --serial <ID_SERIAL_SHORT>

GPIO pin commands take an integer value (`1` to assert, `0` to deassert):

    pytactl oneshot pkey 1 --serial <ID_SERIAL_SHORT>
    pytactl oneshot pkey 0 --serial <ID_SERIAL_SHORT>

The same commands available in the shell can be used here. An unknown command exits with an
error listing the commands supported by your board.

To list the commands supported by your board without running one:

    pytactl listcommands --serial <ID_SERIAL_SHORT>

The board must be connected, as its type is read over USB. To list the commands of a config
file without a board, pass it instead:

    pytactl listcommands --config-file-path <path/to/config.pinout.json>

# Using as a service

    pytactl service --serial <ID_SERIAL_SHORT_1> [<ID_SERIAL_SHORT_2> ...]

The REST API runs on `http://localhost:5000`. Example usage with curl:

    # List connected boards
    curl http://localhost:5000/

    # List available quick methods (bootToEDL, powerOn, etc.)
    curl http://localhost:5000/<boardid>/quick

    # Power on/off
    curl -X PUT http://localhost:5000/<boardid>/quick/powerOn
    curl -X PUT http://localhost:5000/<boardid>/quick/powerOff

    # Boot to EDL
    curl -X PUT http://localhost:5000/<boardid>/quick/bootToEDL

    # Boot to fastboot
    curl -X PUT http://localhost:5000/<boardid>/quick/bootToFastboot

    # Set a named pin
    curl -X PUT "http://localhost:5000/<boardid>/command/reset?value=1"

    # Set a raw pin (e.g., bus A, pin 0)
    curl -X PUT "http://localhost:5000/<boardid>/pin/A0?value=1"

Note: REST API server runs in debug mode. Running with multiple concurrent threads may lead to unexpected behaviour.

# License

pytactl is licensed under the [BSD-3-clause License](https://spdx.org/licenses/BSD-3-Clause.html). See [LICENSE](LICENSE) for the full license text.
