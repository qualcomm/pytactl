# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Conversion between TAC configuration formats.

Upstream qcom-test-automation-controller historically stored one combined
``.tcnf`` file per debug board: hardware pinout, automation script and Qt UI
layout (tabs, buttons, labels, tooltips, grid cells) in a single JSON object.
`PR #54 <https://github.com/qualcomm/qcom-test-automation-controller/pull/54>`_
splits that into two sibling files:

* ``TAC_<CHIP>_<ID>.pinout.json`` - the *shared* hardware description: pins,
  bus map, script variables and the Alpaca script. Self-describing
  (``format == "tac-pinout"``), schema
  ``https://qualcomm.github.io/tac/schemas/pinout-1.0.json``.
* ``TAC_<CHIP>_<ID>.tcnf`` - a slim UI overlay that points back at its pinout
  file through ``pinout_ref`` and carries only UI concerns.

pytactl drives hardware and has no UI, so it wants the pinout half and nothing
else. This module is the Python port of the upstream split (see
``PlatformConfiguration::splitConfiguration``/``mergeConfiguration`` and the
``TACConfigSplit`` utility added by that PR), so pytactl can convert the configs
itself instead of waiting for the split to land upstream, and can consume
upstream's ``*.pinout.json`` files unchanged once it does.

The one place the conversion is deliberately more than a mechanical split is
`drop_shadowed_pins`: ``enabled`` is a UI field and does not survive into the
pinout file, but pytactl needs it, so the decision it used to drive at load time
is baked into the converted pinout instead. See that function for details.
"""

import glob
import json
import logging
import os
import re
import subprocess

logger = logging.getLogger()

# Self-describing envelope of the shared pinout file. Consumers key off
# "format" before parsing anything else.
PINOUT_FORMAT = "tac-pinout"
PINOUT_SCHEMA_VERSION = "1.0"
PINOUT_SCHEMA_URL = "https://qualcomm.github.io/tac/schemas/pinout-1.0.json"
PINOUT_EXTENSION = ".pinout.json"

# Provenance of an imported config: which upstream repository, revision and file
# it was converted from. pytactl's own addition - upstream's pinout schema sets
# "additionalProperties": false, so an annotated config no longer validates
# against it. That is a deliberate trade for traceability of the vendored set:
# every config carries the qcom-test-automation-controller commit it came from,
# so a file stays traceable even away from the directory it was imported into.
SOURCE_FIELD = "source"

# Link from the UI overlay to its pinout file, and the per-pin key the overlay
# uses to join back to a hardware pin.
PINOUT_REF = "pinout_ref"
PIN_REF = "ref"

# Fields that identify a physical pin. Written flat into the pinout file and
# inside the overlay's "ref" object. FTDI uses all three, PSOC/PIC32CX only
# pin_number.
PIN_KEY_FIELDS = ("chip_index", "bus", "pin_number")

# Per-pin hardware fields -> pinout file. Any pin field that is neither a key
# field nor a hardware field (enabled, name, help_hint, group/tab_name,
# command_group, run_priority, and any future UI field) goes to the overlay.
PIN_HARDWARE_FIELDS = (
    "command",
    "input",
    "inverted",
    "initial_value",
    "priority",
    "initialization_priority",
    "classic_action",
)

# Top-level keys that belong only to the shared pinout file.
PINOUT_ONLY_FIELDS = (
    "usb_descriptor",
    "reset_enabled",
    "script",
    "chip_count",
    "bus",
    "supportedFirmwareVer",
)

# Top-level identity keys duplicated into both files (the overlay stays
# authoritative when a config is edited in the QTAC configuration editor).
IDENTITY_FIELDS = ("name", "description", "platform_type", "platform_id")

# Keys that straddle both files and so are handled separately from the
# top-level field partition above.
_SPLIT_ARRAYS = ("pins", "variables")

# Envelope keys, which exist only in the pinout file and are dropped on merge.
# The provenance annotation counts as one: it describes the file, not the board.
_ENVELOPE_FIELDS = ("$schema", "format", "schema_version", SOURCE_FIELD)


class ConfigFormatError(Exception):
    """Raised for a config file that is not in any format we understand."""


def is_pinout(config):
    """True when ``config`` is a parsed shared pinout file."""
    return isinstance(config, dict) and config.get("format") == PINOUT_FORMAT


def is_overlay(config):
    """True when ``config`` is a parsed UI overlay referencing a pinout file."""
    return isinstance(config, dict) and PINOUT_REF in config


def pinout_filename_for(filename):
    """Return the pinout file name for a config file name.

    ``TAC_FTDI_15.tcnf`` -> ``TAC_FTDI_15.pinout.json``. A name that already
    ends in ``.pinout.json`` is returned unchanged, so the mapping is
    idempotent.
    """
    name = os.path.basename(filename)
    if name.endswith(PINOUT_EXTENSION):
        return name
    base, _, extension = name.rpartition(".")
    # rpartition returns ("", "", name) when there is no dot at all.
    return (base or extension) + PINOUT_EXTENSION


def pin_key(key_holder):
    """Stable identity of a physical pin, built from the key fields present.

    ``key_holder`` is either a pin from a pinout file (key fields written flat)
    or the ``ref`` object of an overlay pin. Values are compared as strings
    because ``pin_number`` is a string for FTDI/PSOC and an integer for PIC32CX.
    """
    return tuple(
        str(key_holder[field]) for field in PIN_KEY_FIELDS if field in key_holder
    )


def normalize_script_indentation(script):
    """Indent every statement in an Alpaca script with a single tab.

    TAC config scripts are written with tabs: a ``def`` at column 0 and its body
    one tab in. A handful of upstream configs indent the odd line with spaces
    instead (TAC_FTDI_51, TAC_FTDI_52 and TAC_FTDI_77 each have two), which the
    parser rejects - see ``Board.parse_script``, which requires tabs rather than
    quietly accepting either. Conversion is the import boundary, so that is
    where the indentation is made canonical.

    Blank lines are left alone; they are not statements. Returns
    ``(script, lines_changed)``.
    """
    if not script:
        return script, 0

    changed = 0

    def to_tab(match):
        nonlocal changed
        if match.group(0) != "\t":
            changed += 1
        return "\t"

    # Leading whitespace of a line with content, without touching the line
    # ending: configs use both LF and CRLF and the parser reads both.
    normalized = re.sub(r"^[ \t]+(?=\S)", to_tab, script, flags=re.MULTILINE)
    return normalized, changed


def git_source_info(directory):
    """Describe ``directory`` as a path inside a git checkout, or ``None``.

    Returns ``{"repository": ..., "commit": ..., "path": ...}`` where ``path``
    is ``directory`` relative to the top of the work tree. Used to annotate
    converted configs with the upstream revision they came from, so that
    re-running the conversion against a newer checkout re-stamps them.

    ``None`` when ``directory`` is not in a git checkout, when git is not
    installed, or when the checkout has no commit yet - provenance is recorded
    only when it is actually known.
    """

    def git(*args):
        try:
            result = subprocess.run(
                ["git", "-C", directory, *args],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            logger.debug("git %s failed in %s: %s", args[0], directory, error)
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    toplevel = git("rev-parse", "--show-toplevel")
    commit = git("rev-parse", "HEAD")
    if not toplevel or not commit:
        return None

    source = {}
    remote = git("remote", "get-url", "origin")
    if remote:
        source["repository"] = remote
    source["commit"] = commit
    path = os.path.relpath(os.path.abspath(directory), toplevel)
    if path != ".":
        source["path"] = path.replace(os.sep, "/")
    return source


def annotate_source(pinout, source, filename=None):
    """Return ``pinout`` with a `SOURCE_FIELD` recording where it came from.

    ``source`` is the repository-level provenance (see `git_source_info`);
    ``filename`` names the upstream config file this pinout was converted from.
    The annotation is placed straight after the envelope so it is visible at the
    top of the file, and replaces any annotation already there.
    """
    annotated = {}
    for key in ("$schema", "format", "schema_version"):
        if key in pinout:
            annotated[key] = pinout[key]

    entry = dict(source)
    if filename:
        entry["file"] = filename
    annotated[SOURCE_FIELD] = entry

    for key, value in pinout.items():
        if key not in annotated:
            annotated[key] = value
    return annotated


def split_configuration(combined, pinout_ref=None):
    """Split a combined config into ``(pinout, overlay)``.

    Faithful port of upstream ``PlatformConfiguration::splitConfiguration``:
    identity fields are duplicated, hardware fields go to the pinout, and
    everything else - including any field this port has never heard of - lands
    in the UI overlay, so nothing is silently dropped.

    ``pinout_ref``, when given, is written into the overlay as the link back to
    its pinout file (upstream sets this while saving, not while splitting).
    """
    pinout = {
        "$schema": PINOUT_SCHEMA_URL,
        "format": PINOUT_FORMAT,
        "schema_version": PINOUT_SCHEMA_VERSION,
    }
    overlay = {}

    for key, value in combined.items():
        if key in _SPLIT_ARRAYS:
            continue
        if key in IDENTITY_FIELDS:
            pinout[key] = value
            overlay[key] = value
        elif key in PINOUT_ONLY_FIELDS:
            pinout[key] = value
        else:
            overlay[key] = value

    pinout_pins = []
    overlay_pins = []
    for pin in combined.get("pins", []):
        hardware_pin = {}
        ui_pin = {}
        ref = {}
        for key, value in pin.items():
            if key in PIN_KEY_FIELDS:
                hardware_pin[key] = value
                ref[key] = value
            elif key in PIN_HARDWARE_FIELDS:
                hardware_pin[key] = value
            else:
                ui_pin[key] = value
        ui_pin[PIN_REF] = ref
        pinout_pins.append(hardware_pin)
        overlay_pins.append(ui_pin)

    pinout["pins"] = pinout_pins
    overlay["pins"] = overlay_pins

    # Variable defaults travel with the hardware (the script substitutes them);
    # label/tooltip/type/layout stay in the overlay. Joined back on "name".
    pinout_variables = []
    overlay_variables = []
    for variable in combined.get("variables", []):
        hardware_variable = {}
        ui_variable = {}
        for key, value in variable.items():
            if key == "name":
                hardware_variable[key] = value
                ui_variable[key] = value
            elif key == "default_value":
                hardware_variable[key] = value
            else:
                ui_variable[key] = value
        pinout_variables.append(hardware_variable)
        overlay_variables.append(ui_variable)

    pinout["variables"] = pinout_variables
    overlay["variables"] = overlay_variables

    if pinout_ref:
        overlay[PINOUT_REF] = pinout_ref

    return pinout, overlay


def merge_configuration(pinout, overlay):
    """Rebuild a combined config from a pinout file and its UI overlay.

    Inverse of `split_configuration`, ported from upstream
    ``PlatformConfiguration::mergeConfiguration``. The overlay wins on the
    identity fields both files carry.
    """
    combined = {
        key: value
        for key, value in overlay.items()
        if key not in _SPLIT_ARRAYS and key != PINOUT_REF
    }

    for key, value in pinout.items():
        if key in _SPLIT_ARRAYS or key in _ENVELOPE_FIELDS:
            continue
        combined.setdefault(key, value)

    ui_pins = {pin_key(pin.get(PIN_REF, {})): pin for pin in overlay.get("pins", [])}
    combined_pins = []
    for hardware_pin in pinout.get("pins", []):
        pin = dict(hardware_pin)
        ui_pin = ui_pins.get(pin_key(hardware_pin))
        if ui_pin:
            pin.update({k: v for k, v in ui_pin.items() if k != PIN_REF})
        combined_pins.append(pin)
    combined["pins"] = combined_pins

    ui_variables = {
        variable.get("name"): variable for variable in overlay.get("variables", [])
    }
    combined_variables = []
    for hardware_variable in pinout.get("variables", []):
        variable = dict(hardware_variable)
        ui_variable = ui_variables.get(hardware_variable.get("name"))
        if ui_variable:
            variable.update(ui_variable)
        combined_variables.append(variable)
    combined["variables"] = combined_variables

    return combined


def shadowed_pin_keys(pins):
    """Keys of pins that a same-named enabled pin takes precedence over.

    ``enabled`` is a UI field: it says whether the QTAC app shows a control for
    a pin. It does not survive into the shared pinout file, but pytactl needs
    the distinction, because several configs wire the same command name to two
    physical pins and disable one of them (e.g. ``pshold`` in TAC_FTDI_63,
    ``msmreset`` in TAC_PSOC_66). pytactl binds a command to a pin by
    ``setattr``, so without the flag the *last* pin listed would win, silently
    driving the wrong physical line.

    A disabled pin is only shadowed when its command actually collides with an
    enabled pin; a disabled pin with a command of its own is kept, matching the
    behaviour pytactl applied at load time before the split.
    """
    enabled_commands = {pin.get("command") for pin in pins if pin.get("enabled", True)}
    return {
        pin_key(pin)
        for pin in pins
        if not pin.get("enabled", True) and pin.get("command") in enabled_commands
    }


def convert_configuration(combined):
    """Convert one combined config into the shared pinout config pytactl loads.

    This is `split_configuration` plus the `shadowed_pin_keys` resolution; the
    UI overlay is discarded. Returns the pinout dict.
    """
    pins = combined.get("pins", [])
    shadowed = shadowed_pin_keys(pins)
    pinout, _ = split_configuration(combined)
    if shadowed:
        logger.debug(
            "Dropping %d pin(s) shadowed by an enabled pin with the same command",
            len(shadowed),
        )
        pinout["pins"] = [pin for pin in pinout["pins"] if pin_key(pin) not in shadowed]
    return pinout


def convert_file(path):
    """Load any TAC config file and return it as a pinout config.

    Accepts all three shapes pytactl can be pointed at:

    * a shared ``*.pinout.json`` - returned as-is;
    * a split ``*.tcnf`` UI overlay - its ``pinout_ref`` sibling is loaded from
      the same directory and merged, so the overlay's pin enablement is applied;
    * a legacy combined ``*.tcnf`` - converted in memory.

    Raises `ConfigFormatError` if the file is none of these.
    """
    with open(path) as handle:
        config = json.load(handle)

    if is_pinout(config):
        return config

    if is_overlay(config):
        pinout_path = os.path.join(
            os.path.dirname(path), os.path.basename(config[PINOUT_REF])
        )
        with open(pinout_path) as handle:
            pinout = json.load(handle)
        if not is_pinout(pinout):
            raise ConfigFormatError(
                f"{pinout_path} is referenced as a pinout file but its "
                f"'format' is not {PINOUT_FORMAT!r}"
            )
        converted = convert_configuration(merge_configuration(pinout, config))
        # The merge drops the annotation along with the rest of the envelope;
        # the file it describes is the same one, so put it back.
        if SOURCE_FIELD in pinout:
            converted = annotate_source(converted, pinout[SOURCE_FIELD])
        return converted

    if "pins" not in config:
        raise ConfigFormatError(f"{path} is not a TAC configuration file")

    return convert_configuration(config)


def rewrite_device_list(device_list, default_filename):
    """Point every catalog entry at the converted pinout file.

    Upstream ``devicelist.json`` entries carry repository-relative paths
    (``../../configurations/TAC_FTDI_15.tcnf``), and leave ``configPath`` empty
    for boards that rely on the generated default FTDI config. Every config is
    installed flat into one directory, so entries are reduced to the bare
    pinout file name, and empty ones point at ``default_filename``.

    Mutates ``device_list`` in place and returns the number of entries changed.
    """
    patched = 0
    for entry in device_list.get("catalog", []):
        config_path = entry.get("configPath")
        new_path = pinout_filename_for(config_path) if config_path else default_filename
        if new_path != config_path:
            entry["configPath"] = new_path
            patched += 1
    return patched


def _write_json(path, obj):
    with open(path, "w") as handle:
        json.dump(obj, handle, indent=4)
        handle.write("\n")


def convert_directory(
    source,
    destination=None,
    default_filename=None,
    write_overlay=False,
    dry_run=False,
    source_info=None,
    annotate=True,
):
    """Convert every TAC config in ``source`` into ``destination``.

    Script indentation is normalised to tabs on the way through, so that what
    is written out is what `Board.parse_script` requires.

    Each ``*.tcnf`` (legacy or already split) becomes a ``*.pinout.json``.
    ``*.pinout.json`` files that have no ``*.tcnf`` beside them are converted
    too, so a directory of upstream pinout files can be installed directly.
    A ``devicelist.json`` in ``source`` is copied over with its ``configPath``
    entries rewritten to the converted file names.

    With ``write_overlay`` the slim UI overlay is written alongside each pinout
    file, giving the complete two-file layout rather than just the half pytactl
    needs. With ``dry_run`` nothing is written.

    Each converted config is annotated with the upstream repository, revision
    and file it came from (see `annotate_source`). ``source_info`` supplies that
    provenance and always wins; when it is omitted and ``source`` is a git
    checkout, provenance is read from the checkout and applied only to configs
    that do not already carry an annotation - so re-converting an imported set
    in place does not restamp it with the wrong repository. Pass
    ``annotate=False`` to write configs that carry no annotation and so validate
    against upstream's pinout schema unchanged; a config that is already
    annotated then keeps the annotation it has.

    Returns a ``{"converted": [...], "failed": [(name, error), ...],
    "device_list": bool}`` summary.
    """
    destination = destination or source
    default_filename = default_filename or ("default" + PINOUT_EXTENSION)
    # Provenance the caller states is authoritative; provenance merely observed
    # from the source directory is not, and defers to any already recorded.
    stated_source = source_info is not None
    if annotate and source_info is None:
        source_info = git_source_info(source)
        if source_info is None:
            logger.debug(
                "%s is not a git checkout; converting without provenance", source
            )
    if not dry_run:
        os.makedirs(destination, exist_ok=True)

    converted = []
    failed = []

    sources = sorted(glob.glob(os.path.join(source, "*.tcnf")))
    # Pinout files whose overlay is missing (or that were never split at all)
    # are not reachable through a .tcnf, so pick them up directly.
    covered = {pinout_filename_for(path) for path in sources}
    sources += [
        path
        for path in sorted(glob.glob(os.path.join(source, "*" + PINOUT_EXTENSION)))
        if os.path.basename(path) not in covered
    ]

    for path in sources:
        name = os.path.basename(path)
        pinout_name = pinout_filename_for(name)
        try:
            with open(path) as handle:
                config = json.load(handle)
            if is_pinout(config):
                pinout, overlay = config, None
            elif is_overlay(config):
                pinout = convert_file(path)
                overlay = None
            else:
                pinout, overlay = split_configuration(config, pinout_ref=pinout_name)
                shadowed = shadowed_pin_keys(config.get("pins", []))
                if shadowed:
                    pinout["pins"] = [
                        pin for pin in pinout["pins"] if pin_key(pin) not in shadowed
                    ]
                    overlay["pins"] = [
                        pin
                        for pin in overlay["pins"]
                        if pin_key(pin.get(PIN_REF, {})) not in shadowed
                    ]
        except (OSError, ValueError, ConfigFormatError) as error:
            logger.error("Failed to convert %s: %s", name, error)
            failed.append((name, str(error)))
            continue

        script, retabbed = normalize_script_indentation(pinout.get("script", ""))
        if retabbed:
            logger.info("%s: re-indented %d script line(s) with tabs", name, retabbed)
            pinout["script"] = script

        if annotate and source_info and (stated_source or SOURCE_FIELD not in pinout):
            pinout = annotate_source(pinout, source_info, name)

        if not dry_run:
            _write_json(os.path.join(destination, pinout_name), pinout)
            if write_overlay and overlay is not None:
                _write_json(os.path.join(destination, name), overlay)
        logger.debug("Converted %s -> %s", name, pinout_name)
        converted.append(pinout_name)

    device_list_path = os.path.join(source, "devicelist.json")
    has_device_list = os.path.isfile(device_list_path)
    if has_device_list:
        with open(device_list_path) as handle:
            device_list = json.load(handle)
        patched = rewrite_device_list(device_list, default_filename)
        logger.debug("Rewrote %d configPath entries in devicelist.json", patched)
        if not dry_run:
            _write_json(os.path.join(destination, "devicelist.json"), device_list)

    return {
        "converted": converted,
        "failed": failed,
        "device_list": has_device_list,
    }
