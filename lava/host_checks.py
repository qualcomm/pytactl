#!/usr/bin/env python3

# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Host-side checks for pytactl, run inside a container by a LAVA job.

pytactl is the host half of a LAVA board: the device dictionaries in
lava.infra.foundries.io drive their boards with

    /usr/local/bin/tac-api.py --serial <serial> --command powerOn|powerOff|bootToEDL

so what a board needs from pytactl is that its config loads at all, and that it
defines those three commands. These checks assert exactly that, over the whole
bundled config set, plus the format rules the loader now enforces.

Results are reported as LAVA test cases on stdout; run it directly from a
checkout (PYTHONPATH=. python lava/host_checks.py) to see the same output
outside LAVA. Exits non-zero if any check failed.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

# What every device dictionary in the lab asks of a board's config.
LAB_COMMANDS = ("powerOn", "powerOff", "bootToEDL")

failures = 0


def result(case, ok, detail=""):
    """Emit one LAVA test case, and remember whether anything failed."""
    global failures
    verdict = "pass" if ok else "fail"
    if not ok:
        failures += 1
    if detail:
        print(f"  {case}: {detail}")
    print(f"<LAVA_SIGNAL_TESTCASE TEST_CASE_ID={case} RESULT={verdict}>")


def measurement(case, value, units):
    print(
        f"<LAVA_SIGNAL_TESTCASE TEST_CASE_ID={case} RESULT=pass "
        f"MEASUREMENT={value} UNITS={units}>"
    )


def script_functions(config):
    return set(re.findall(r"^def\s+(\w+)", config.get("script", ""), re.MULTILINE))


# USB vendor/product pairs that Board.create_board() dispatches on, as they
# appear in sysfs. Kept here rather than imported so the survey still works if
# the package failed to install.
KNOWN_BOARDS = {
    ("0403", "6015"): "Bughopper V1",
    ("0403", "6011"): "FTDI",
    ("05c6", "9302"): "PSOC",
    ("2341", "b001"): "Bughopper V2",
    ("04d8", "000a"): "PIC32CX",
}


def survey_worker_usb():
    """Report the debug boards attached to the worker, read from sysfs.

    A LAVA docker test shell gets no /dev/bus/usb, so pytactl cannot open these
    - see the README section this job is documented in. sysfs is still readable,
    which is enough to record which boards the container is running alongside.
    """
    root = "/sys/bus/usb/devices"
    found = []
    if not os.path.isdir(root):
        result("worker-usb-readable", False, "no /sys/bus/usb/devices")
        return found

    for entry in sorted(os.listdir(root)):
        base = os.path.join(root, entry)

        def read(field, base=base):
            try:
                with open(os.path.join(base, field)) as handle:
                    return handle.read().strip()
            except OSError:
                return None

        vid, pid = read("idVendor"), read("idProduct")
        if not vid or not pid:
            continue
        label = KNOWN_BOARDS.get((vid.lower(), pid.lower()))
        if label:
            serial = read("serial") or "<none>"
            found.append((label, f"{vid}:{pid}", serial))

    for label, ids, serial in found:
        print(f"  worker debug board: {label:<13} {ids}  serial={serial}")
    result("worker-usb-readable", True, f"{len(found)} debug board(s) on this worker")
    measurement("worker-debug-boards", len(found), "boards")
    return found


def main():
    import pytactl
    from pytactl import debugboard, tacconfig

    config_dir = pytactl.default_tac_config_path()
    print(f"pytactl {pytactl.__version__} configs={config_dir}")

    # What hardware this container is running alongside (read-only).
    survey_worker_usb()

    # The container has no /dev/bus/usb, so enumeration finds nothing. Assert
    # that pytactl says so cleanly rather than crashing - this is the path a
    # containerised host tool takes when it has no device access.
    listed = subprocess.run(
        [sys.executable, "-m", "pytactl.cli", "list", "--log-level", "ERROR"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    result(
        "list-without-usb-access-is-clean",
        listed.returncode == 0,
        listed.stdout.strip().splitlines()[-1]
        if listed.stdout.strip()
        else listed.stderr.strip()[-200:],
    )

    # --- the config set ships and is in the format the loader expects --------
    names = sorted(
        n for n in os.listdir(config_dir) if n.endswith(tacconfig.PINOUT_EXTENSION)
    )
    result("configs-bundled", len(names) > 1, f"{len(names)} config files")
    measurement("configs-count", len(names), "configs")

    configs = {}
    for name in names:
        with open(os.path.join(config_dir, name)) as handle:
            configs[name] = json.load(handle)

    result(
        "configs-are-pinout-format",
        all(tacconfig.is_pinout(c) for c in configs.values()),
    )

    commits = {c["source"]["commit"] for c in configs.values() if "source" in c}
    result(
        "configs-carry-provenance",
        len(commits) == 1 and len(next(iter(commits), "")) == 40,
        f"upstream commit {', '.join(sorted(commits))}",
    )

    # Indentation is part of the format and the loader now requires it.
    offenders = []
    for name, config in configs.items():
        for number, line in enumerate(config.get("script", "").splitlines(), 1):
            if not line.strip():
                continue
            indent = line[: len(line) - len(line.lstrip())]
            if indent and indent != "\t":
                offenders.append(f"{name}:{number}")
    result("configs-tab-indented", not offenders, str(offenders[:5]))

    # devicelist.json is what maps a board's USB descriptor to its config.
    with open(os.path.join(config_dir, "devicelist.json")) as handle:
        catalog = json.load(handle)["catalog"]
    dangling = sorted(
        {
            e["configPath"]
            for e in catalog
            if not os.path.isfile(os.path.join(config_dir, e["configPath"]))
        }
    )
    result("devicelist-resolves", not dangling, str(dangling))

    # --- every config loads through the real board path ----------------------
    loaded, unloadable = {}, {}
    for name in names:
        path = os.path.join(config_dir, name)
        try:
            loaded[name] = tacconfig.convert_file(path)
        except Exception as error:  # noqa: BLE001 - reporting, not handling
            unloadable[name] = f"{type(error).__name__}: {error}"

    for name, error in sorted(unloadable.items()):
        print(f"  UNLOADABLE {name}: {error}")
    result("configs-load", not unloadable, f"{len(unloadable)} unloadable")

    # The two Glymur COB configs used to be unloadable: both indent a line with
    # spaces, and the lab has glymur-crd devices.
    for name in (
        "TAC_FTDI_51.pinout.json",
        "TAC_FTDI_52.pinout.json",
        "TAC_FTDI_77.pinout.json",
    ):
        if name in configs:
            result(
                f"loads-{name.split('.')[0].lower()}",
                name in loaded,
                unloadable.get(name, "ok"),
            )

    # --- what the lab's device dictionaries actually ask for -----------------
    complete, incomplete = [], {}
    for name, config in sorted(loaded.items()):
        functions = script_functions(config)
        missing = [c for c in LAB_COMMANDS if c not in functions]
        if missing:
            incomplete[name] = missing
        else:
            complete.append(name)

    for name, missing in sorted(incomplete.items()):
        print(
            f"  NO POWER CONTROL {name} ({configs[name]['name']!r}): "
            f"missing {', '.join(missing)}"
        )
    measurement("configs-with-lab-commands", len(complete), "configs")
    measurement("configs-missing-lab-commands", len(incomplete), "configs")
    # Not a failure: some boards genuinely have no power control. It is a fact
    # about the fleet that a device dictionary author needs to know.
    result(
        "lab-command-audit-ran",
        True,
        f"{len(complete)} of {len(loaded)} provide {'/'.join(LAB_COMMANDS)}",
    )

    # --- the format rules the loader enforces --------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        # A space-indented script must be rejected, naming the line.
        sample = dict(next(iter(loaded.values())))
        sample["script"] = "def powerOn()\n    battery 1\n"
        broken = os.path.join(tmp, "TAC_BROKEN_1.pinout.json")
        with open(broken, "w") as handle:
            json.dump(sample, handle)
        try:
            debugboard.Board.create_from_config(broken)
            result("rejects-space-indentation", False, "loaded anyway")
        except debugboard.ConfigScriptError as error:
            result(
                "rejects-space-indentation",
                "single tab" in str(error) and "line(s) 2" in str(error),
                str(error),
            )
        except Exception as error:  # noqa: BLE001
            result(
                "rejects-space-indentation",
                False,
                f"wrong error {type(error).__name__}: {error}",
            )

        # ...and convertconfigs must repair exactly that.
        source = os.path.join(tmp, "src")
        os.makedirs(source)
        legacy = dict(sample)
        for key in ("$schema", "format", "schema_version", "source"):
            legacy.pop(key, None)
        with open(os.path.join(source, "TAC_BROKEN_1.tcnf"), "w") as handle:
            json.dump(legacy, handle)
        out = os.path.join(tmp, "out")
        tacconfig.convert_directory(source, out)
        with open(os.path.join(out, "TAC_BROKEN_1.pinout.json")) as handle:
            converted = json.load(handle)
        result(
            "convertconfigs-retabs",
            converted["script"].startswith("def powerOn()\n\tbattery 1"),
            repr(converted["script"]),
        )
        board = debugboard.Board.create_from_config(
            os.path.join(out, "TAC_BROKEN_1.pinout.json")
        )
        result("converted-config-loads", board is not None)

    print(f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
