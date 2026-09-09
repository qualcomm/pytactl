# pytactl test suite

Tests that every config file of the TAC config set can be loaded through the
same code path `pytactl.shell` and `pytactl.service` use — `Board.create_board()` — with
the USB device, GPIO and serial hardware mocked, and that each board exposes the
required quick methods (`powerOn`, `powerOff`, `bootToEDL`). Alongside that,
`tests/test_tacconfig.py` covers the config conversion itself.

The code under test lives in the `pytactl` package (`pytactl/debugboard.py` etc.), so
the tests import it as `from pytactl import debugboard`.

## Configs under test

The data-driven tests run over the `.pinout.json` files of a TAC config set.
`tests/conftest.py` looks for one in three places, in order — the same precedence
`pytactl` itself applies, plus an environment override on top:

1. `$PYTACTL_TAC_CONFIG_DIR`, if set — an explicit override, for distro packagers
   and anyone who unpacks a config set somewhere of their own choosing.
2. `pytactl.INSTALLED_TAC_CONFIG_PATH` — a newer upstream set pulled with
   `pytactl installconfigs`, so the suite tests against that when it exists (the
   `config-loading` CI workflow does exactly this).
3. `pytactl.PACKAGE_TAC_CONFIG_PATH` — the config set vendored in this repository
   under `pytactl/tac_configs/`, which is what a plain checkout tests against.

So a bare `python -m pytest` in a fresh checkout runs the full suite offline, against
the bundled configs. To test against current upstream instead:

```sh
pytactl installconfigs
```

or

```sh
pytactl convertconfigs /path/to/upstream/configurations --output ~/tac-configs
export PYTACTL_TAC_CONFIG_DIR=~/tac-configs
```

If none of the three holds a `.pinout.json` file — a distro build that strips the
bundled configs, say — every config-dependent test **skips** with a reason naming all
three, and the remainder of the suite (imports, CLI parser, config conversion,
`create_board()` dispatch for Bughopper, …) still runs — see
[issue #22](https://github.com/qualcomm/pytactl/issues/22). The handful of tests that
are specifically *about* the vendored set skip on their own marker,
`requires_bundled_configs`, so de-vendoring is not a build failure.

## Running

```sh
. ./venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

The exact pass/xfail counts track the config set under test; a run against the
bundled set is in the order of **284 passed, 1 skipped, 25 xfailed**. With the
configs stripped, expect roughly **77 passed, 17 skipped**.

## How it works

The tests are data-driven over every `*.pinout.json` file of the config set. For each
config a fake USB device is constructed so that `Board.create_board()`'s vendor/product
dispatch and config-by-USB-description matching select that config:

- **FTDI boards** are matched by `usb_descriptor` against the USB device's
  `product` string. The harness writes a synthetic `devicelist.json` mapping each
  config to a unique descriptor and sets the fake device's `product` to match.
- **PSOC boards** are matched by `platform_id`, normally read from the board over
  a serial console. The harness mocks `PsocBoard.__get_board_id` to return the
  synthetic id registered for that config.

Hardware is replaced with harmless fakes (`mock_hardware` fixture):
`GpioAsyncController` → `MagicMock`, `PsocPort` → in-memory fake, `sleep` → no-op
(so config scripts with `delay` run instantly).

### Config-loading tests (`tests/test_config_loading.py`)

| Test | Asserts |
|------|---------|
| `test_config_loads_via_usb_dispatch` | the config parses and builds a board |
| `test_required_functions_available` | `powerOn`/`powerOff`/`bootToEDL` are present and callable |
| `test_required_functions_execute` | invoking the defined functions runs the parsed script end to end |
| `test_quick_method_call_wrapper` | the REST API `QuickMethod.call()` path works |
| `test_ftdi_falls_back_to_default_config` | an unknown FTDI descriptor falls back to `default.pinout.json` (the bundled FTDI Alpaca-Lite config, platform_id 13) |

### Conversion tests (`tests/test_tacconfig.py`)

Cover `pytactl.tacconfig`, the converter from the upstream config files to the shared
pinout format: the field partition between the pinout file and the UI overlay, the
merge back to a combined config, the shadowed-pin resolution that stands in for the
`enabled` flag the pinout format drops, `devicelist.json` rewriting, directory
conversion (in place, to an output directory, dry run, with the UI overlay, and with a
broken config in the source), and that a board built from each of the three accepted
config shapes binds the same pins.

They also cover the provenance annotation: where it is placed, that it replaces rather
than accumulates, that it is treated as envelope (so it never leaks into a merged config),
that `convertconfigs` reads it from the source checkout but does not restamp an
already-imported set, and that `--no-annotate` leaves it off.

Finally they check the vendored config set itself: every bundled file is a valid pinout
config, no `devicelist.json` entry points at a config the package does not ship, and every
config names the same upstream commit — the one `pytactl/tac_configs/README.md` records,
so the two cannot drift.

### Script parsing tests (`tests/test_script_parsing.py`)

Cover `Board.parse_script` against small synthetic configs: a tab-indented script
parses, anything else (spaces, mixed, over-indented) is rejected with the offending
line numbers, CRLF line endings parse, blank lines are not held to the tab rule,
script variables are substituted and an undeclared one is reported by name, and a
command the script drives with no pin behind it is warned about without mistaking
`delay`, `logComment` or a call to another function for one.

### Dispatch tests (`tests/test_create_board.py`)

`Board.create_board()` routing: no device → `None`, FTDI, PSOC, Bughopper V1,
Bughopper V2 with/without the optional `hid` module, and PIC32CX.

## Special-cased configs

These are declared centrally in `tests/conftest.py`, each with a reason. All
`xfail` markers are `strict=True`, so if a config is fixed it surfaces as an
**XPASS**, prompting the entry to be removed.

### Excluded from the suite (`EXCLUDED_CONFIGS`)

Not driven by the config-script path:

| Config | Reason |
|--------|--------|
| `TAC_PIC32CXAuto_54.pinout.json` | PIC32CXAuto uses a dedicated dispatch path (covered by `test_create_board_dispatches_pic32cx`) |
| `TAC_FTDI_80.pinout.json` | Bughopper board, handled by `BughopperV1Board`/`BughopperV2Board` |

### Expected failures (`xfail`)

All of these are upstream config data problems. Each needs a value that only the
board's owner can supply — which physical pin a command drives, or how long a board
must be held in a state — so they are left as upstream ships them and fixes belong in
[qcom-test-automation-controller](https://github.com/qualcomm/qcom-test-automation-controller).
pytactl reports each of them at load time rather than failing obscurely later.

**Fail to load** (`XFAIL_LOAD`):

| Config | Reason |
|--------|--------|
| `TAC_FTDI_72.pinout.json` | script uses `$edl`/`$uefi`/`$fastboot` but the config declares no variables. Raises `ConfigScriptError` naming them; the delays are board timings only the config can supply |

**Omit a required function** (`XFAIL_REQUIRED`) — load fine but don't define all
three of `powerOn`/`powerOff`/`bootToEDL`:

| Config | Reason |
|--------|--------|
| `TAC_FTDI_15.pinout.json` | defines `bootToEDL` only; no `powerOn`/`powerOff` |
| `TAC_FTDI_16.pinout.json` | no `powerOn`/`powerOff` |
| `TAC_FTDI_41.pinout.json` | two SoCs: `fpowerOn`/`spowerOn`, `bootToAPQEDL`/`bootToSDXEDL`. Which one a plain `powerOn` should mean is a board question |
| `TAC_FTDI_42.pinout.json` | empty script (SMART LABEL board defines no functions) |
| `TAC_FTDI_60.pinout.json` | defines `reset`/`bootToEDL`/`bootToUEFI` only; no power control |
| `TAC_PSOC_24.pinout.json` | defines `bootToEDL` variants only; no `powerOn`/`powerOff` |
| `TAC_PSOC_31.pinout.json` | `bootToNADEDL`/`bootToEAPEDL` for two subsystems; no plain `bootToEDL` |

Note that `powerOn` is **not** an alias for the `powerOnTheDevice` most of these do
define. Across the 28 configs that define both, `powerOn` calls `powerOnTheDevice` and
*then* presses the power key for a board-specific hold time (800 ms, 3500 ms, …).
Synthesising the missing one would mean inventing a power-up sequence, so the suite
records the gap instead.

**Fail to execute** (`XFAIL_EXECUTE`) — define the function, but its script drives a
command that no pin in the config defines, so the bound method raises `AttributeError`
when invoked. pytactl names the commands in a warning at load time:

| Config | Drives, with no pin behind it |
|--------|-------------------------------|
| `TAC_FTDI_23`, `TAC_FTDI_69` | `pkey`, `voldn`, `volup` — M.2 modem cards with no buttons |
| `TAC_FTDI_29` | `battery`, `pedl`, `pkey`, `sedl`, `usb0`, `voldn`, `volup` — a phone script on an RF switch box, whose only pins are `VC1`–`VC3` |
| `TAC_FTDI_56` | `usb1` — board only defines `usb0` |
| `TAC_FTDI_65`, `TAC_FTDI_73` | `sedl`, `usb1` |
| `TAC_FTDI_67` | `sedl`, `usb1`, and `sumxs2` — which looks like a transposition typo for the board's own `smuxs2` |
| `TAC_FTDI_72` | listed here too, but never gets that far — it fails to load |

## Fixed rather than carried

Three configs (`TAC_FTDI_51`, `TAC_FTDI_52`, `TAC_FTDI_77`) used to be carried as
`XFAIL_LOAD` for "wrong indentation": each indents two script lines with spaces where
the format calls for a tab, which made the whole config unloadable.

The fix is in the config files, not in the parser. Config scripts indent statements with
a single tab, `installconfigs`/`convertconfigs` re-indent them on import (logging each
file they touch), and the parser *requires* the tab — a config that does not follow the
format is reported against, with the offending line numbers, rather than quietly
accepted. All three now load and run. See `tests/test_script_parsing.py` for the parser
side and `test_normalize_script_indentation` / `test_bundled_scripts_are_tab_indented`
in `tests/test_tacconfig.py` for the import side.

## Notes

- `TAC_PSOC_31` loads because `parse_script` renames pin commands that are not
  valid Python identifiers (e.g. `12vpoweroff` → `_12vpoweroff`) consistently
  in the script and the pin command they bind to.
- The `XFAIL_EXECUTE` configs would raise `AttributeError` if their
  `powerOn`/etc. were called on real hardware. pytactl warns about this at load
  time, naming the commands, so it is visible before a board is driven.
