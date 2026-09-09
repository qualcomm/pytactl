# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for :mod:`pytactl.tacconfig`, the converter from the upstream TAC
configuration files to the shared pinout format pytactl loads.

The split mirrors the one upstream introduces in
`PR #54 <https://github.com/qualcomm/qcom-test-automation-controller/pull/54>`_:
hardware description and script go to ``*.pinout.json``, UI layout stays in the
``*.tcnf`` overlay. These tests pin down the field partition, the round trip
back to a combined config, and the one place the conversion adds something of
its own: dropping pins that a same-named enabled pin shadows.
"""

import glob
import json
import os
import pathlib
import subprocess

import pytest
from conftest import requires_bundled_configs

import pytactl
from pytactl import debugboard, tacconfig

# A miniature legacy combined config exercising every partition rule: identity
# fields, pinout-only fields, UI-only fields, an unknown field, FTDI pin keys,
# a shadowed pin pair, and a script variable.
LEGACY_CONFIG = {
    # identity - duplicated into both files
    "name": "Test Board",
    "description": "Test Board description",
    "platform_type": "FTDI",
    "platform_id": 999,
    # pinout-only
    "usb_descriptor": "TEST DEBUG BOARD",
    "reset_enabled": False,
    "chip_count": 1,
    "bus": [{"chip_index": 0, "bus": "C", "bus_function": 2}],
    "script": "def powerOn()\n\tbattery 0\n",
    # UI-only
    "author": "somebody",
    "fileVersion": 1,
    "creation_date": "Wed Feb 15 10:59:13 2023",
    "modification_date": "Wed Feb 15 17:10:24 2023",
    "tabs": [
        {
            "name": "General",
            "user_tab": False,
            "moveable": False,
            "visible": True,
            "configurable": True,
            "ordinal": 0,
        }
    ],
    "buttons": [
        {
            "name": "EDL",
            "command": "bootToEDL",
            "command_group": 0,
            "cellLocation": "0,0",
            "tab": "General",
            "tooltip": "Boot to EDL",
        }
    ],
    # a field neither side knows about: must survive, in the overlay
    "some_future_ui_field": 42,
    "pins": [
        {
            "chip_index": 0,
            "bus": "C",
            "pin_number": "0",
            "command": "battery",
            "input": False,
            "inverted": False,
            "initial_value": False,
            "priority": 0,
            "enabled": True,
            "name": "Battery",
            "help_hint": "Battery disconnect",
            "group": "General",
            "command_group": 3,
            "run_priority": "0,0",
        },
        {
            # same command as the pin above but disabled: shadowed
            "chip_index": 0,
            "bus": "C",
            "pin_number": "1",
            "command": "battery",
            "input": False,
            "inverted": True,
            "initial_value": False,
            "priority": 1,
            "enabled": False,
            "name": "Battery (old)",
            "help_hint": "<add a tooltip>",
            "group": "General",
            "command_group": 3,
            "run_priority": "0,1",
        },
        {
            # disabled but its command collides with nothing: kept
            "chip_index": 0,
            "bus": "C",
            "pin_number": "2",
            "command": "pkey",
            "input": False,
            "inverted": False,
            "initial_value": False,
            "priority": 2,
            "enabled": False,
            "name": "Power key",
            "help_hint": "<add a tooltip>",
            "group": "General",
            "command_group": 3,
            "run_priority": "0,2",
        },
    ],
    "variables": [
        {
            "name": "edl",
            "default_value": "100",
            "label": "EDL timing (ms)",
            "tooltip": "How long to hold EDL",
            "type": 1,
            "cellLocation": "0,0",
        }
    ],
}


def pin_by_number(pins, number):
    return next(p for p in pins if str(p.get("pin_number")) == str(number))


def write_json(path, obj):
    path.write_text(json.dumps(obj))
    return str(path)


# --------------------------------------------------------------------------
# File naming
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("TAC_FTDI_15.tcnf", "TAC_FTDI_15.pinout.json"),
        ("/some/dir/TAC_PSOC_7.tcnf", "TAC_PSOC_7.pinout.json"),
        # idempotent: an already-converted name maps to itself
        ("TAC_FTDI_15.pinout.json", "TAC_FTDI_15.pinout.json"),
        ("no_extension", "no_extension.pinout.json"),
    ],
)
def test_pinout_filename_for(name, expected):
    assert tacconfig.pinout_filename_for(name) == expected


# --------------------------------------------------------------------------
# Splitting
# --------------------------------------------------------------------------


def test_split_writes_self_describing_envelope():
    """Third-party tools key off ``format`` before parsing anything else."""
    pinout, overlay = tacconfig.split_configuration(LEGACY_CONFIG)

    assert pinout["format"] == tacconfig.PINOUT_FORMAT
    assert pinout["schema_version"] == tacconfig.PINOUT_SCHEMA_VERSION
    assert pinout["$schema"] == tacconfig.PINOUT_SCHEMA_URL
    assert tacconfig.is_pinout(pinout)
    # The envelope belongs to the pinout file alone.
    assert not tacconfig.is_pinout(overlay)


def test_split_partitions_top_level_fields():
    pinout, overlay = tacconfig.split_configuration(LEGACY_CONFIG)

    for field in tacconfig.IDENTITY_FIELDS:
        assert pinout[field] == LEGACY_CONFIG[field]
        assert overlay[field] == LEGACY_CONFIG[field]

    for field in ("usb_descriptor", "reset_enabled", "chip_count", "bus", "script"):
        assert pinout[field] == LEGACY_CONFIG[field]
        assert field not in overlay

    for field in ("author", "fileVersion", "tabs", "buttons"):
        assert overlay[field] == LEGACY_CONFIG[field]
        assert field not in pinout


def test_split_sends_unknown_fields_to_the_overlay():
    """An unrecognised field is UI baggage until proven otherwise, so it lands
    in the overlay rather than being dropped."""
    pinout, overlay = tacconfig.split_configuration(LEGACY_CONFIG)

    assert overlay["some_future_ui_field"] == 42
    assert "some_future_ui_field" not in pinout


def test_split_partitions_pin_fields():
    pinout, overlay = tacconfig.split_configuration(LEGACY_CONFIG)

    hardware = pin_by_number(pinout["pins"], 0)
    assert hardware == {
        "chip_index": 0,
        "bus": "C",
        "pin_number": "0",
        "command": "battery",
        "input": False,
        "inverted": False,
        "initial_value": False,
        "priority": 0,
    }

    ui = next(p for p in overlay["pins"] if p["ref"]["pin_number"] == "0")
    assert ui["ref"] == {"chip_index": 0, "bus": "C", "pin_number": "0"}
    assert ui["enabled"] is True
    assert ui["name"] == "Battery"
    assert ui["help_hint"] == "Battery disconnect"
    # No hardware field leaks into the overlay pin.
    assert not set(ui) & set(tacconfig.PIN_HARDWARE_FIELDS)


def test_split_partitions_variables():
    """The script substitutes ``default_value``, so it travels with the pinout;
    the label/tooltip/layout stay behind. ``name`` is the join key, so both
    sides keep it."""
    pinout, overlay = tacconfig.split_configuration(LEGACY_CONFIG)

    assert pinout["variables"] == [{"name": "edl", "default_value": "100"}]
    assert overlay["variables"] == [
        {
            "name": "edl",
            "label": "EDL timing (ms)",
            "tooltip": "How long to hold EDL",
            "type": 1,
            "cellLocation": "0,0",
        }
    ]


def test_split_always_emits_pins_and_variables():
    """Both arrays are required by the pinout schema even when the legacy config
    never declared them (most FTDI configs have no variables at all)."""
    pinout, overlay = tacconfig.split_configuration({"name": "bare"})

    assert pinout["pins"] == []
    assert pinout["variables"] == []
    assert overlay["pins"] == []
    assert overlay["variables"] == []


def test_split_writes_pinout_ref_into_the_overlay():
    _, overlay = tacconfig.split_configuration(
        LEGACY_CONFIG, pinout_ref="TAC_FTDI_999.pinout.json"
    )

    assert overlay[tacconfig.PINOUT_REF] == "TAC_FTDI_999.pinout.json"
    assert tacconfig.is_overlay(overlay)


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


def test_merge_round_trips_a_split_config():
    pinout, overlay = tacconfig.split_configuration(
        LEGACY_CONFIG, pinout_ref="TAC_FTDI_999.pinout.json"
    )

    assert tacconfig.merge_configuration(pinout, overlay) == LEGACY_CONFIG


def test_merge_drops_the_envelope_and_the_pinout_ref():
    pinout, overlay = tacconfig.split_configuration(
        LEGACY_CONFIG, pinout_ref="TAC_FTDI_999.pinout.json"
    )

    combined = tacconfig.merge_configuration(pinout, overlay)

    for key in ("$schema", "format", "schema_version", tacconfig.PINOUT_REF):
        assert key not in combined


def test_merge_drops_the_provenance_annotation():
    """The annotation describes the file, not the board, so it is envelope: it
    must not leak into a merged config and from there into the UI overlay on the
    next split."""
    pinout, overlay = tacconfig.split_configuration(LEGACY_CONFIG)
    annotated = tacconfig.annotate_source(pinout, {"commit": "deadbeef"}, "x.tcnf")

    combined = tacconfig.merge_configuration(annotated, overlay)

    assert tacconfig.SOURCE_FIELD not in combined
    assert combined == tacconfig.merge_configuration(pinout, overlay)


def test_merge_prefers_the_overlay_on_identity_fields():
    """Both files carry the identity fields; the overlay is authoritative,
    because that is the one the QTAC configuration editor writes."""
    pinout, overlay = tacconfig.split_configuration(LEGACY_CONFIG)
    overlay["name"] = "Renamed Board"

    combined = tacconfig.merge_configuration(pinout, overlay)

    assert combined["name"] == "Renamed Board"


def test_merge_ignores_an_overlay_pin_with_no_hardware_counterpart():
    """The pinout file drives the pin list: a UI entry for a pin that is not in
    it (e.g. one this converter dropped) does not resurrect the pin."""
    pinout, overlay = tacconfig.split_configuration(LEGACY_CONFIG)
    pinout["pins"] = [p for p in pinout["pins"] if p["pin_number"] != "1"]

    combined = tacconfig.merge_configuration(pinout, overlay)

    assert [p["pin_number"] for p in combined["pins"]] == ["0", "2"]


# --------------------------------------------------------------------------
# Shadowed pins - the enablement decision baked into the conversion
# --------------------------------------------------------------------------


def test_shadowed_pin_keys_finds_the_disabled_duplicate():
    shadowed = tacconfig.shadowed_pin_keys(LEGACY_CONFIG["pins"])

    assert shadowed == {("0", "C", "1")}


def test_shadowed_pin_keys_keeps_a_disabled_pin_with_its_own_command():
    """Disabled pins are not dropped wholesale - only when an enabled pin would
    otherwise be overwritten by one."""
    pins = [
        {"pin_number": "0", "command": "battery", "enabled": True},
        {"pin_number": "1", "command": "pkey", "enabled": False},
    ]

    assert tacconfig.shadowed_pin_keys(pins) == set()


def test_shadowed_pin_keys_ignores_duplicate_enabled_pins():
    """Two enabled pins sharing a command are a config quirk this conversion
    does not arbitrate; it only resolves enabled-vs-disabled."""
    pins = [
        {"pin_number": "0", "command": "eud", "enabled": True},
        {"pin_number": "1", "command": "eud", "enabled": True},
    ]

    assert tacconfig.shadowed_pin_keys(pins) == set()


def test_convert_configuration_drops_shadowed_pins():
    pinout = tacconfig.convert_configuration(LEGACY_CONFIG)

    numbers = [p["pin_number"] for p in pinout["pins"]]
    assert numbers == ["0", "2"]
    # The surviving 'battery' pin is the enabled one (inverted False, not the
    # disabled pin 1 which is inverted).
    assert pin_by_number(pinout["pins"], 0)["inverted"] is False


# --------------------------------------------------------------------------
# Loading a config file in any of the three shapes
# --------------------------------------------------------------------------


def test_convert_file_reads_a_legacy_combined_config(tmp_path):
    path = write_json(tmp_path / "TAC_FTDI_999.tcnf", LEGACY_CONFIG)

    pinout = tacconfig.convert_file(path)

    assert tacconfig.is_pinout(pinout)
    assert [p["pin_number"] for p in pinout["pins"]] == ["0", "2"]


def test_convert_file_returns_a_pinout_file_unchanged(tmp_path):
    expected = tacconfig.convert_configuration(LEGACY_CONFIG)
    path = write_json(tmp_path / "TAC_FTDI_999.pinout.json", expected)

    assert tacconfig.convert_file(path) == expected


def test_convert_file_resolves_a_split_pair(tmp_path):
    """Pointed at the UI overlay of an already-split upstream config, the loader
    picks up the sibling pinout file and applies the overlay's pin enablement -
    so pytactl gets the same pin set either way."""
    pinout, overlay = tacconfig.split_configuration(
        LEGACY_CONFIG, pinout_ref="TAC_FTDI_999.pinout.json"
    )
    write_json(tmp_path / "TAC_FTDI_999.pinout.json", pinout)
    overlay_path = write_json(tmp_path / "TAC_FTDI_999.tcnf", overlay)

    loaded = tacconfig.convert_file(overlay_path)

    assert loaded == tacconfig.convert_configuration(LEGACY_CONFIG)


def test_convert_file_rejects_a_dangling_pinout_ref(tmp_path):
    """A ``pinout_ref`` pointing at something that is not a pinout file is an
    error, not silently treated as a config."""
    write_json(tmp_path / "TAC_FTDI_999.pinout.json", {"pins": []})
    overlay_path = write_json(
        tmp_path / "TAC_FTDI_999.tcnf", {"pinout_ref": "TAC_FTDI_999.pinout.json"}
    )

    with pytest.raises(tacconfig.ConfigFormatError):
        tacconfig.convert_file(overlay_path)


def test_convert_file_rejects_an_unrelated_json_file(tmp_path):
    path = write_json(tmp_path / "devicelist.json", {"catalog": []})

    with pytest.raises(tacconfig.ConfigFormatError):
        tacconfig.convert_file(path)


# --------------------------------------------------------------------------
# devicelist.json
# --------------------------------------------------------------------------


def test_rewrite_device_list_points_entries_at_the_pinout_files():
    device_list = {
        "catalog": [
            # repository-relative path -> bare converted file name
            {"configPath": "../../configurations/TAC_FTDI_15.tcnf"},
            # empty -> the generated FTDI Alpaca-Lite default
            {"configPath": ""},
            # already converted -> left alone
            {"configPath": "TAC_PSOC_7.pinout.json"},
        ]
    }

    patched = tacconfig.rewrite_device_list(device_list, "default.pinout.json")

    assert [e["configPath"] for e in device_list["catalog"]] == [
        "TAC_FTDI_15.pinout.json",
        "default.pinout.json",
        "TAC_PSOC_7.pinout.json",
    ]
    assert patched == 2


# --------------------------------------------------------------------------
# Directory conversion (what "convertconfigs" and "installconfigs" drive)
# --------------------------------------------------------------------------


@pytest.fixture
def legacy_dir(tmp_path):
    """A source directory in the shape upstream ships today."""
    source = tmp_path / "configurations"
    source.mkdir()
    write_json(source / "TAC_FTDI_999.tcnf", LEGACY_CONFIG)
    write_json(
        source / "devicelist.json",
        {
            "catalog": [
                {"configPath": "../../configurations/TAC_FTDI_999.tcnf"},
                {"configPath": ""},
            ]
        },
    )
    return source


def test_convert_directory_writes_pinout_files_and_devicelist(legacy_dir, tmp_path):
    destination = tmp_path / "installed"

    result = tacconfig.convert_directory(
        str(legacy_dir), str(destination), default_filename="default.pinout.json"
    )

    assert result["converted"] == ["TAC_FTDI_999.pinout.json"]
    assert result["failed"] == []
    assert result["device_list"] is True

    pinout = json.loads((destination / "TAC_FTDI_999.pinout.json").read_text())
    assert tacconfig.is_pinout(pinout)
    assert [p["pin_number"] for p in pinout["pins"]] == ["0", "2"]

    device_list = json.loads((destination / "devicelist.json").read_text())
    assert [e["configPath"] for e in device_list["catalog"]] == [
        "TAC_FTDI_999.pinout.json",
        "default.pinout.json",
    ]

    # Only the pinout half is installed unless the overlay is asked for.
    assert not (destination / "TAC_FTDI_999.tcnf").exists()


def test_convert_directory_can_write_the_ui_overlay(legacy_dir, tmp_path):
    destination = tmp_path / "installed"

    tacconfig.convert_directory(str(legacy_dir), str(destination), write_overlay=True)

    overlay = json.loads((destination / "TAC_FTDI_999.tcnf").read_text())
    assert overlay[tacconfig.PINOUT_REF] == "TAC_FTDI_999.pinout.json"
    assert overlay["tabs"] == LEGACY_CONFIG["tabs"]
    # The overlay drops the pin the pinout dropped, so the pair stays consistent.
    assert [p["ref"]["pin_number"] for p in overlay["pins"]] == ["0", "2"]


def test_convert_directory_dry_run_writes_nothing(legacy_dir, tmp_path):
    destination = tmp_path / "installed"

    result = tacconfig.convert_directory(
        str(legacy_dir), str(destination), dry_run=True
    )

    assert result["converted"] == ["TAC_FTDI_999.pinout.json"]
    assert not destination.exists()


def test_convert_directory_converts_in_place_by_default(legacy_dir):
    tacconfig.convert_directory(str(legacy_dir))

    assert (legacy_dir / "TAC_FTDI_999.pinout.json").is_file()


def test_convert_directory_is_idempotent(legacy_dir, tmp_path):
    """Running the converter over its own output must not change it - the
    installed set can be re-converted, and an already-split upstream is handled
    the same as a legacy one."""
    destination = tmp_path / "installed"
    tacconfig.convert_directory(str(legacy_dir), str(destination))
    first = (destination / "TAC_FTDI_999.pinout.json").read_text()

    tacconfig.convert_directory(str(destination), str(destination))

    assert (destination / "TAC_FTDI_999.pinout.json").read_text() == first


def test_convert_directory_reports_a_broken_config(legacy_dir, tmp_path):
    """One unparseable config does not abort the whole directory."""
    (legacy_dir / "TAC_BROKEN_1.tcnf").write_text("{not json")
    destination = tmp_path / "installed"

    result = tacconfig.convert_directory(str(legacy_dir), str(destination))

    assert result["converted"] == ["TAC_FTDI_999.pinout.json"]
    assert [name for name, _ in result["failed"]] == ["TAC_BROKEN_1.tcnf"]


def test_convert_directory_without_a_devicelist(tmp_path):
    source = tmp_path / "configurations"
    source.mkdir()
    write_json(source / "TAC_FTDI_999.tcnf", LEGACY_CONFIG)

    result = tacconfig.convert_directory(str(source), str(tmp_path / "installed"))

    assert result["device_list"] is False


# --------------------------------------------------------------------------
# Script indentation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "script", "expected", "changed"),
    [
        ("tabs are left alone", "def a()\n\tbattery 1\n", "def a()\n\tbattery 1\n", 0),
        ("spaces", "def a()\n    battery 1\n", "def a()\n\tbattery 1\n", 1),
        (
            "over-indented",
            "def a()\n\t\tbattery 1\n",
            "def a()\n\tbattery 1\n",
            1,
        ),
        (
            "crlf is preserved",
            "def a()\r\n    battery 1\r\n",
            "def a()\r\n\tbattery 1\r\n",
            1,
        ),
        ("blank lines untouched", "def a()\n   \n\tb 1\n", "def a()\n   \n\tb 1\n", 0),
        ("empty script", "", "", 0),
    ],
)
def test_normalize_script_indentation(name, script, expected, changed):
    """Conversion is the import boundary, so it is where a script is made to
    indent with tabs - what `Board.parse_script` requires."""
    assert tacconfig.normalize_script_indentation(script) == (expected, changed)


def test_convert_directory_re_indents_scripts_with_tabs(tmp_path):
    """TAC_FTDI_51, TAC_FTDI_52 and TAC_FTDI_77 each indent two lines with
    spaces upstream; importing them yields configs pytactl will load."""
    source = tmp_path / "configurations"
    source.mkdir()
    spaced = dict(LEGACY_CONFIG, script="def powerOn()\n    battery 1\n\tpkey 1\n")
    write_json(source / "TAC_FTDI_999.tcnf", spaced)
    destination = tmp_path / "installed"

    tacconfig.convert_directory(str(source), str(destination))

    script = json.loads((destination / "TAC_FTDI_999.pinout.json").read_text())[
        "script"
    ]
    assert script == "def powerOn()\n\tbattery 1\n\tpkey 1\n"


@requires_bundled_configs
def test_bundled_scripts_are_tab_indented():
    """Every statement in every shipped config is indented with exactly one tab,
    which is what the parser requires; nothing relies on it being lenient."""
    offenders = []
    for path in glob.glob(
        os.path.join(pytactl.PACKAGE_TAC_CONFIG_PATH, "*" + tacconfig.PINOUT_EXTENSION)
    ):
        with open(path) as handle:
            script = json.load(handle)["script"]
        for number, line in enumerate(script.splitlines(), 1):
            if not line.strip():
                continue
            indent = line[: len(line) - len(line.lstrip())]
            if indent and indent != "\t":
                offenders.append(f"{os.path.basename(path)}:{number}")

    assert not offenders, f"not indented with a single tab: {offenders}"


# --------------------------------------------------------------------------
# Provenance annotation
# --------------------------------------------------------------------------


def git_checkout(tmp_path, remote="https://example.invalid/qtac.git"):
    """Make a one-commit git checkout and return (path, commit)."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for args in (
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["remote", "add", "origin", remote],
    ):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    (tmp_path / "marker").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "marker"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return tmp_path, commit


def test_annotate_source_records_the_origin_after_the_envelope():
    """The annotation goes straight after the envelope so it reads at the top of
    the file, and the config data below it is untouched."""
    pinout = tacconfig.convert_configuration(LEGACY_CONFIG)

    annotated = tacconfig.annotate_source(
        pinout,
        {"repository": "https://example.invalid/qtac", "commit": "abc123"},
        "TAC_FTDI_999.tcnf",
    )

    assert list(annotated)[:4] == [
        "$schema",
        "format",
        "schema_version",
        tacconfig.SOURCE_FIELD,
    ]
    assert annotated[tacconfig.SOURCE_FIELD] == {
        "repository": "https://example.invalid/qtac",
        "commit": "abc123",
        "file": "TAC_FTDI_999.tcnf",
    }
    assert {k: v for k, v in annotated.items() if k != tacconfig.SOURCE_FIELD} == pinout


def test_annotate_source_replaces_an_earlier_annotation():
    """Re-importing a config from a newer revision restamps it rather than
    accumulating annotations."""
    pinout = tacconfig.convert_configuration(LEGACY_CONFIG)
    first = tacconfig.annotate_source(pinout, {"commit": "old"}, "x.tcnf")

    second = tacconfig.annotate_source(first, {"commit": "new"}, "x.tcnf")

    assert second[tacconfig.SOURCE_FIELD] == {"commit": "new", "file": "x.tcnf"}


def test_annotate_source_without_a_filename():
    pinout = tacconfig.convert_configuration(LEGACY_CONFIG)

    annotated = tacconfig.annotate_source(pinout, {"commit": "abc123"})

    assert annotated[tacconfig.SOURCE_FIELD] == {"commit": "abc123"}


def test_git_source_info_reads_the_checkout(tmp_path):
    checkout, commit = git_checkout(tmp_path)
    configurations = checkout / "configurations"
    configurations.mkdir()

    source = tacconfig.git_source_info(str(configurations))

    assert source == {
        "repository": "https://example.invalid/qtac.git",
        "commit": commit,
        "path": "configurations",
    }


def test_git_source_info_omits_the_path_at_the_top_of_the_checkout(tmp_path):
    checkout, commit = git_checkout(tmp_path)

    assert tacconfig.git_source_info(str(checkout)) == {
        "repository": "https://example.invalid/qtac.git",
        "commit": commit,
    }


def test_git_source_info_returns_none_outside_a_checkout(tmp_path):
    """Provenance is recorded only when it is actually known."""
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()

    assert tacconfig.git_source_info(str(plain)) is None


def test_convert_directory_annotates_from_the_source_checkout(tmp_path):
    checkout, commit = git_checkout(tmp_path / "qtac")
    configurations = checkout / "configurations"
    configurations.mkdir()
    write_json(configurations / "TAC_FTDI_999.tcnf", LEGACY_CONFIG)
    destination = tmp_path / "installed"

    tacconfig.convert_directory(str(configurations), str(destination))

    source = json.loads((destination / "TAC_FTDI_999.pinout.json").read_text())[
        tacconfig.SOURCE_FIELD
    ]
    assert source["commit"] == commit
    assert source["path"] == "configurations"
    assert source["file"] == "TAC_FTDI_999.tcnf"


def test_convert_directory_uses_the_source_info_it_is_given(legacy_dir, tmp_path):
    """What the caller states wins: "installconfigs" knows the repository and
    the commit it resolved the ref to, which no local checkout can supply."""
    destination = tmp_path / "installed"
    stated = {
        "repository": "https://example.invalid/qtac",
        "ref": "main",
        "commit": "0" * 40,
    }

    tacconfig.convert_directory(str(legacy_dir), str(destination), source_info=stated)

    assert json.loads((destination / "TAC_FTDI_999.pinout.json").read_text())[
        tacconfig.SOURCE_FIELD
    ] == {**stated, "file": "TAC_FTDI_999.tcnf"}


def test_convert_directory_can_skip_annotating(legacy_dir, tmp_path):
    """Configs written with annotate=False validate against upstream's pinout
    schema unchanged, which forbids the extra key."""
    destination = tmp_path / "installed"

    tacconfig.convert_directory(str(legacy_dir), str(destination), annotate=False)

    pinout = json.loads((destination / "TAC_FTDI_999.pinout.json").read_text())
    assert tacconfig.SOURCE_FIELD not in pinout


def test_convert_directory_does_not_restamp_an_imported_set(tmp_path):
    """Re-converting an already-imported set from inside another checkout must
    keep the provenance it has: the observed repository is not where those
    configs came from."""
    imported = tmp_path / "vendored"
    imported.mkdir()
    original = tacconfig.annotate_source(
        tacconfig.convert_configuration(LEGACY_CONFIG),
        {"repository": "https://example.invalid/qtac", "commit": "0" * 40},
        "TAC_FTDI_999.tcnf",
    )
    write_json(imported / "TAC_FTDI_999.pinout.json", original)
    git_checkout(tmp_path)  # the *wrong* repository, observed around it

    tacconfig.convert_directory(str(imported))

    assert (
        json.loads((imported / "TAC_FTDI_999.pinout.json").read_text())[
            tacconfig.SOURCE_FIELD
        ]
        == original[tacconfig.SOURCE_FIELD]
    )


def test_convert_file_keeps_the_annotation_of_a_split_pair(tmp_path):
    """Resolving an overlay to its pinout goes through a merge, which drops the
    envelope; the annotation describes the same file either way, so it survives."""
    pinout, overlay = tacconfig.split_configuration(
        LEGACY_CONFIG, pinout_ref="TAC_FTDI_999.pinout.json"
    )
    annotated = tacconfig.annotate_source(pinout, {"commit": "abc123"}, "x.tcnf")
    write_json(tmp_path / "TAC_FTDI_999.pinout.json", annotated)
    overlay_path = write_json(tmp_path / "TAC_FTDI_999.tcnf", overlay)

    loaded = tacconfig.convert_file(overlay_path)

    assert loaded[tacconfig.SOURCE_FIELD] == {"commit": "abc123", "file": "x.tcnf"}


# --------------------------------------------------------------------------
# The bundled default, and the board built from a converted config
# --------------------------------------------------------------------------


@requires_bundled_configs
def test_bundled_default_config_is_a_pinout_file():
    path = os.path.join(
        pytactl.PACKAGE_TAC_CONFIG_PATH, pytactl.BUNDLED_DEFAULT_CONFIG_FILENAME
    )

    with open(path) as handle:
        config = json.load(handle)

    assert tacconfig.is_pinout(config)
    assert config["platform_id"] == 13
    assert pytactl.DEFAULT_CONFIG_FILENAME.endswith(tacconfig.PINOUT_EXTENSION)


@requires_bundled_configs
def test_bundled_config_set_is_all_pinout_files():
    """Every config vendored into the package is in the format pytactl loads."""
    paths = glob.glob(
        os.path.join(pytactl.PACKAGE_TAC_CONFIG_PATH, "*" + tacconfig.PINOUT_EXTENSION)
    )

    assert len(paths) > 1, "the bundled config set is missing"
    for path in paths:
        with open(path) as handle:
            config = json.load(handle)
        assert tacconfig.is_pinout(config), path
        assert config["schema_version"] == tacconfig.PINOUT_SCHEMA_VERSION, path


@requires_bundled_configs
def test_bundled_configs_record_the_upstream_commit():
    """Every vendored config says which qcom-test-automation-controller commit
    it was imported from, and they all name the same one - a set assembled from
    two different revisions is an import that went wrong."""
    commits = set()
    for path in glob.glob(
        os.path.join(pytactl.PACKAGE_TAC_CONFIG_PATH, "*" + tacconfig.PINOUT_EXTENSION)
    ):
        with open(path) as handle:
            source = json.load(handle).get(tacconfig.SOURCE_FIELD)
        assert source, f"{os.path.basename(path)} is not annotated"
        assert "qcom-test-automation-controller" in source["repository"], path
        commits.add(source["commit"])

    assert len(commits) == 1, f"mixed upstream revisions: {sorted(commits)}"
    commit = commits.pop()
    assert len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)

    # The directory's own README quotes the same commit, so the two cannot drift.
    readme = pathlib.Path(pytactl.PACKAGE_TAC_CONFIG_PATH, "README.md").read_text()
    assert commit in readme


@requires_bundled_configs
def test_bundled_devicelist_resolves_to_bundled_configs():
    """No catalog entry points at a config the package does not ship - including
    the entries with no config of their own, which point at the FTDI
    Alpaca-Lite default under its bundled name."""
    with open(os.path.join(pytactl.PACKAGE_TAC_CONFIG_PATH, "devicelist.json")) as f:
        device_list = json.load(f)

    catalog = device_list["catalog"]
    assert catalog
    for entry in catalog:
        config_path = entry["configPath"]
        assert config_path.endswith(tacconfig.PINOUT_EXTENSION), entry
        assert os.path.isfile(
            os.path.join(pytactl.PACKAGE_TAC_CONFIG_PATH, config_path)
        ), f"devicelist.json points at a missing config: {config_path}"


def test_default_config_file_prefers_the_installed_copy(tmp_path):
    """A directory populated by "installconfigs" holds the FTDI Alpaca-Lite
    default under the generic name; that copy wins."""
    (tmp_path / pytactl.DEFAULT_CONFIG_FILENAME).write_text("{}")
    (tmp_path / pytactl.BUNDLED_DEFAULT_CONFIG_FILENAME).write_text("{}")

    assert pytactl.default_config_file(str(tmp_path)) == str(
        tmp_path / pytactl.DEFAULT_CONFIG_FILENAME
    )


def test_default_config_file_accepts_the_bundled_name(tmp_path):
    """The config set shipped with the package keeps the config under its own
    name rather than duplicating it, so that name is resolved too."""
    (tmp_path / pytactl.BUNDLED_DEFAULT_CONFIG_FILENAME).write_text("{}")

    assert pytactl.default_config_file(str(tmp_path)) == str(
        tmp_path / pytactl.BUNDLED_DEFAULT_CONFIG_FILENAME
    )


def test_default_config_file_returns_none_when_absent(tmp_path):
    assert pytactl.default_config_file(str(tmp_path)) is None


def test_board_binds_the_enabled_pin_not_the_shadowed_one(tmp_path):
    """The regression the shadowed-pin handling exists to prevent.

    ``enabled`` does not survive into the pinout file, and pytactl binds a
    command to a pin with ``setattr``, so a disabled duplicate listed after the
    enabled pin would silently take over the command and drive the wrong
    physical line.
    """
    path = write_json(tmp_path / "TAC_FTDI_999.tcnf", LEGACY_CONFIG)

    board = debugboard.Board.create_from_config(path)

    assert sorted(board.pins) == ["0", "2"]
    assert board.pins["0"].command == "battery"
    # 'battery' resolves to the enabled pin's setter, not the disabled pin's.
    assert board.battery == board.pins["0"].set


def test_board_loads_an_already_split_config_pair(tmp_path):
    """Pointing pytactl at an upstream split pair yields the same board as the
    legacy combined config it came from."""
    pinout, overlay = tacconfig.split_configuration(
        LEGACY_CONFIG, pinout_ref="TAC_FTDI_999.pinout.json"
    )
    write_json(tmp_path / "TAC_FTDI_999.pinout.json", pinout)
    overlay_path = write_json(tmp_path / "TAC_FTDI_999.tcnf", overlay)

    board = debugboard.Board.create_from_config(overlay_path)

    assert sorted(board.pins) == ["0", "2"]
    assert callable(getattr(board, "powerOn", None))


def test_board_creates_ports_from_the_platform_type(tmp_path):
    """The pinout config names the platform, so the port layout no longer has to
    be inferred from the config file's name."""
    path = write_json(tmp_path / "unrecognisable-name.json", LEGACY_CONFIG)

    board = debugboard.Board.create_from_config(path)

    # bus C is declared with bus_function 2, so it becomes a port.
    assert sorted(board.ports) == ["C"]
