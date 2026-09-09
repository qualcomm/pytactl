# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `pytactl convertconfigs <dir>` converts a directory of TAC config files into
  the shared pinout format, in place or into `--output <dir>`, with `--dry-run`
  and `--write-overlay`.
- `pytactl.tacconfig`, the module behind that conversion: it splits a combined
  config into the hardware pinout and the UI overlay, merges them back, and
  loads a config file in any of the three shapes.
- The full board config set is now added into `pytactl/tac_configs/` and ships
  with the package, so FTDI, PSOC and PIC32CX boards can be driven straight
  after install with no `installconfigs` step and no network access.
  `pytactl/tac_configs/README.md` records the upstream commit it was taken from
  and how to refresh it. `installconfigs` still pulls the current upstream set
  when you want something newer than the snapshot, and what it installs keeps
  taking precedence over the bundled set.
- Converted configs are annotated with where they came from: a `source` object
  naming the repository, the commit (resolved from the ref at import time, so
  it does not move), the directory and the upstream file. `convertconfigs`
  reads this from the git checkout it is pointed at and `installconfigs` from
  the config repository, so a refresh re-stamps the set; an already annotated
  config is left alone rather than restamped from wherever it now sits.
- `convertconfigs --default-config <name>` sets the file that `devicelist.json`
  entries with no config of their own point at, for a destination that holds
  the FTDI Alpaca-Lite default under a name other than `default.pinout.json` -
  as the bundled set does.

### Fixed

- `TAC_FTDI_51`, `TAC_FTDI_52` and `TAC_FTDI_77` now load. Each indents two
  script lines with spaces where the config format calls for a tab, which made
  the whole config unloadable. `installconfigs` and `convertconfigs` re-indent
  scripts with tabs as they import them, logging each file they touch, so the
  bundled set and anything imported through either command is clean.

### Changed

- pytactl now loads board configs from `*.pinout.json` - the hardware half of
  the upstream config split - instead of the combined `.tcnf` files. The UI
  half (tabs, buttons, labels, tooltips, grid cells) is no longer parsed at
  all. A single config file passed with `--config-file-path` may still be a
  legacy combined `.tcnf`, or the UI overlay of a split pair; both are
  converted on load.
- `pytactl installconfigs` converts what it downloads, so the installed config
  set is `.pinout.json` files plus a `devicelist.json` whose `configPath`
  entries point at them. It is no longer needed to drive a board, but a config
  directory left by an earlier version holds `.tcnf` files that are no longer
  found: re-run `installconfigs`, convert it in place with
  `pytactl convertconfigs <dir>`, or delete it to fall back to the bundled set.
- The default FTDI Alpaca-Lite config is `TAC_FTDI_13.pinout.json` in the
  bundled set, and is installed by `installconfigs` as `default.pinout.json`
  rather than `default.tcnf`. Either name is accepted wherever that config is
  looked up, so a config directory does not have to carry both.
- The port layout of a board driven from a single config file is derived from
  the config's `platform_type` instead of being guessed from the file name, so
  a config file may now be named anything.
- A script that uses a variable the config never declares (`delay $edl` with no
  `edl` in `variables`) now raises `ConfigScriptError` naming the variables and
  what the config does declare, instead of reaching `exec()` as an opaque
  `SyntaxError`. The value is a board timing only the config can supply, so
  pytactl says so rather than guessing one.
- A config script must indent statements with a single tab. The parser rejects
  anything else with a `ConfigScriptError` naming the offending lines, instead
  of accepting whatever whitespace is there: indentation is part of the config
  file format, and a config that breaks it is one to fix. A raw upstream
  `.tcnf` passed to `--config-file-path` may therefore be rejected where the
  converted config loads - convert the directory first. A config directory
  installed by an earlier version may need `installconfigs` re-running.
- Loading a config warns when its script drives a command no pin defines,
  naming the commands. Several upstream configs carry lines for hardware the
  board does not have; that used to surface only as an `AttributeError` from
  inside the script, typically when someone tried to power a board on.
- The rule that a pin disabled in the UI gives way to an enabled pin sharing
  its command name now lives in the conversion rather than in board loading.
  `enabled` is a UI field and does not survive into the pinout file, so the
  decision is made once, up front; the resulting pin set is unchanged.

## [2.0] - 2026-08-21

The first release published to [PyPI](https://pypi.org/project/pytactl/). This
release turns a pair of in-tree scripts into an installable command line tool,
so it carries a number of breaking changes for anyone upgrading from 1.4.

### Breaking

- The project and its command are renamed from `pytac` to `pytactl`. The old
  name collides with an unrelated package on PyPI (Diamond Light Source's
  Python Toolkit for Accelerator Controls), so the project could never be
  published under it. Existing installs must switch to the `pytactl` command,
  and the installed config set moves from the `pytac` per-user data directory
  (`~/.local/share/pytac` on Linux) to `pytactl` - re-run
  `pytactl installconfigs` or copy the old directory over.
- Run modes are now subcommands rather than mutually exclusive flags:
  `pytactl shell`, `pytactl oneshot` and `pytactl service` replace
  `--shell`, `--oneshot` and `--service`. Each subcommand has its own help
  and argument set.
- The flat modules were restructured into an importable `pytactl` package and
  are driven through a single console entry point, replacing the previous
  per-script invocation from a checkout.
- Dependencies are declared in `pyproject.toml` instead of `requirements.txt`,
  which is removed. The `pyudev` dependency is dropped in favour of `pyserial`,
  and `platformdirs` is added.

### Added

- Packaging metadata and a console entry point, making the tool installable
  with `pip` and `pipx` and runnable from anywhere.
- `pytactl list` enumerates connected debug boards with their type and serial
  number, so the value needed for `--serial` is discoverable from the tool
  itself instead of `udevadm` or `lsusb`.
- `pytactl installconfigs` downloads every `.tcnf` file and `devicelist.json`
  from the TAC config repository into a local directory (the per-user data
  directory by default). The `--config-repository`, `--local-path`, `--ref`
  and `--repository-path` flags override the source repository, install
  location, git ref and in-repo path.
- `pytactl oneshot` runs a single command against a board and exits, for use
  in CI jobs, provisioning and other scripted automation.
- Support for the PIC32CX board used on automotive platforms.
- A pytest suite: smoke tests covering the package version, the default config
  path and the CLI parser, plus a CI job that pulls the upstream configs and
  checks that `powerOn`, `powerOff` and `bootToEDL` load and run for each one.
- A `Makefile` with `lint`, `format`, `test` and `install-dev` targets, so
  contributors and CI run the same checks.
- `SECURITY.md` documenting how to report a vulnerability, and a workflow that
  builds and publishes releases to PyPI via Trusted Publishing.

### Changed

- Board detection uses `pyserial`'s `serial.tools.list_ports` instead of
  `pyudev`, which depends on Linux-only `libudev`. Board enumeration, PIC32CX
  detection and serial port resolution now work on macOS and Windows as well
  as Linux.
- `--serial` may be omitted when exactly one board is connected; the tool
  assumes that board, matching how `adb` and `fastboot` behave.
- The default config directory prefers the config set installed by
  `installconfigs` when it is populated, falling back to the configs bundled
  with the package.
- The FTDI board fallback prefers `default.tcnf`, keeping `TAC_FTDI_13.tcnf`
  for the bundled case.
- `ruff` replaces `black`, `isort` and `pylint` for formatting, import
  ordering and linting, collapsing three CI jobs into one.
- The README documents `pipx` installation, the native `hidapi` system
  dependency for Ubuntu/Debian and Fedora, the udev rules needed for
  Bughopper V2 (notably under WSL2), and the `installconfigs` and `oneshot`
  subcommands.

### Fixed

- GPIO commands on Windows: a `0x00` HID report-ID placeholder is prepended to
  each write, since hidapi consumes byte 0 as the report ID. Without it the
  `CMD_GPIO` byte was stripped and the board never rebooted, which went
  unnoticed on Linux/hidraw.
- Config script parsing now handles `//` comments, and pin and function names
  that are not valid Python identifiers (for example `12vpoweroff`), so config
  files can be used unchanged.
- Pins marked as disabled but still referenced by a script are handled without
  losing the name-collision behaviour that keeps disabled pins out.
- `except:` was replaced with `except Exception:` so `KeyboardInterrupt` and
  `SystemExit` are no longer swallowed.

## [1.4] - 2026-06-10

Earlier releases are recorded in the git history and in the
[GitHub releases](https://github.com/qualcomm/pytactl/releases) page.

[2.0]: https://github.com/qualcomm/pytactl/compare/v1.4...v2.0
[1.4]: https://github.com/qualcomm/pytactl/releases/tag/v1.4
