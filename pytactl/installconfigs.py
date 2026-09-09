# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

import logging
import os
import shutil
import sys
import tempfile
from urllib.parse import urlparse

import requests

from . import (
    BUNDLED_DEFAULT_CONFIG_FILENAME,
    DEFAULT_CONFIG_FILENAME,
    DEFAULT_CONFIG_REPOSITORY,
    INSTALLED_TAC_CONFIG_PATH,
    PACKAGE_TAC_CONFIG_PATH,
    tacconfig,
)

logger = logging.getLogger()

# Default subdirectory in the config repository holding the config files +
# devicelist.json, and the default git ref to fetch them from.
DEFAULT_REPOSITORY_PATH = "configurations"
DEFAULT_REF = "HEAD"

# The synthesized FTDI Alpaca-Lite config shipped with the package. It has no
# upstream config file, so we copy it into the install directory as the default.
_BUNDLED_DEFAULT = os.path.join(
    PACKAGE_TAC_CONFIG_PATH, BUNDLED_DEFAULT_CONFIG_FILENAME
)


def _parse_owner_repo(repository_url):
    """Extract (owner, repo) from a GitHub repository URL."""
    path = urlparse(repository_url).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) < 2:
        raise ValueError(
            f"Cannot parse owner/repo from repository URL: {repository_url}"
        )
    return parts[0], parts[1]


def _list_config_files(owner, repo, repository_path, ref):
    """Return [(name, download_url)] for every config file and devicelist.json.

    Both config formats are fetched: legacy combined ``.tcnf`` files and, once
    upstream splits them, the ``.pinout.json`` files plus their slim ``.tcnf``
    overlays. `tacconfig.convert_directory` reduces whichever arrives to the
    pinout files pytactl loads.
    """
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{repository_path}"
    resp = requests.get(api_url, params={"ref": ref}, timeout=30)
    resp.raise_for_status()
    files = []
    for entry in resp.json():
        name = entry.get("name", "")
        if entry.get("type") == "file" and (
            name.endswith(".tcnf")
            or name.endswith(tacconfig.PINOUT_EXTENSION)
            or name == "devicelist.json"
        ):
            files.append((name, entry["download_url"]))
    return files


def _resolve_commit(owner, repo, ref):
    """Resolve a git ref to the commit SHA it points at, or ``None``.

    Converted configs are annotated with the revision they came from, and a ref
    like "HEAD" or "main" moves; recording the SHA it resolved to at import time
    is what makes the annotation worth having.
    """
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
    try:
        resp = requests.get(api_url, timeout=30)
        resp.raise_for_status()
        return resp.json().get("sha")
    except (requests.RequestException, ValueError) as error:
        logger.warning("Could not resolve %s to a commit: %s", ref, error)
        return None


def _download(url, dest):
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        f.write(resp.content)


def install_configs(
    config_repository=None,
    local_path=None,
    ref=None,
    repository_path=None,
    annotate=True,
):
    """Download TAC config files, convert them and install them into ``local_path``.

    Fetches every config file and ``devicelist.json`` from ``repository_path``
    (at git ``ref``) of ``config_repository``, converts each one into the shared
    pinout format pytactl loads (see :mod:`pytactl.tacconfig`), copies the
    bundled FTDI Alpaca-Lite config in as the default, and rewrites the
    ``configPath`` entries in ``devicelist.json`` to match.

    Each installed config is annotated with the repository, ref and commit it
    came from, unless ``annotate`` is false.
    """
    config_repository = config_repository or DEFAULT_CONFIG_REPOSITORY
    local_path = local_path or INSTALLED_TAC_CONFIG_PATH
    ref = ref or DEFAULT_REF
    repository_path = repository_path or DEFAULT_REPOSITORY_PATH

    owner, repo = _parse_owner_repo(config_repository)
    os.makedirs(local_path, exist_ok=True)

    logger.info(
        "Fetching config list from %s/%s/%s at %s",
        owner,
        repo,
        repository_path,
        ref,
    )
    files = _list_config_files(owner, repo, repository_path, ref)
    if not files:
        logger.error(
            "No config files or devicelist.json found in %s", config_repository
        )
        sys.exit(1)

    source_info = None
    if annotate:
        source_info = {"repository": config_repository, "ref": ref}
        commit = _resolve_commit(owner, repo, ref)
        if commit:
            source_info["commit"] = commit
            logger.info("Importing %s at %s", ref, commit)
        source_info["path"] = repository_path

    with tempfile.TemporaryDirectory(prefix="pytactl-configs-") as download_dir:
        for name, url in files:
            logger.info("Downloading %s", name)
            _download(url, os.path.join(download_dir, name))

        result = tacconfig.convert_directory(
            download_dir,
            local_path,
            default_filename=DEFAULT_CONFIG_FILENAME,
            source_info=source_info,
            annotate=annotate,
        )

    if result["failed"]:
        for name, error in result["failed"]:
            logger.warning("Skipped %s: %s", name, error)
    if not result["device_list"]:
        logger.warning("devicelist.json not found in repository; not installed")

    shutil.copyfile(_BUNDLED_DEFAULT, os.path.join(local_path, DEFAULT_CONFIG_FILENAME))
    logger.info("Installed %s", DEFAULT_CONFIG_FILENAME)

    print(f"Installed {len(result['converted']) + 1} TAC config files to {local_path}")


def convert_configs(
    source,
    destination=None,
    write_overlay=False,
    dry_run=False,
    annotate=True,
    default_config=None,
):
    """Convert a directory of TAC config files into the shared pinout format.

    Backs the "convertconfigs" subcommand: the same conversion
    "installconfigs" applies to what it downloads, run against a checkout (or
    any directory) the caller already has.

    When ``source`` is inside a git checkout, each converted config is annotated
    with that repository and the commit checked out; ``annotate=False`` skips it.

    ``default_config`` names the file that ``devicelist.json`` entries with no
    config of their own point at, for a destination that holds the FTDI
    Alpaca-Lite default under a name other than DEFAULT_CONFIG_FILENAME.
    """
    if not os.path.isdir(source):
        logger.error("Source directory does not exist: %s", source)
        sys.exit(1)

    destination = destination or source
    result = tacconfig.convert_directory(
        source,
        destination,
        default_filename=default_config or DEFAULT_CONFIG_FILENAME,
        write_overlay=write_overlay,
        dry_run=dry_run,
        annotate=annotate,
    )

    for name, error in result["failed"]:
        logger.warning("Skipped %s: %s", name, error)

    verb = "Would convert" if dry_run else "Converted"
    print(f"{verb} {len(result['converted'])} TAC config files into {destination}")
    if result["failed"]:
        print(f"{len(result['failed'])} config file(s) could not be converted")
        sys.exit(1)
