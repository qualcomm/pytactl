# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for the REST interface in :mod:`pytactl.service`.

The service is driven through Flask's test client against a real FtdiBoard,
built by ``Board.create_board()`` from the bundled default config, the same
way ``run_service()`` builds it. Only the USB and GPIO layers are faked (see
``mock_hardware`` in conftest), so a PUT goes all the way down to the GPIO
write.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import make_usb_device

from pytactl import debugboard, service

SERIAL = "S1"


@pytest.fixture
def board(prepared_configs, patch_usb_find, monkeypatch):
    """Register a mocked FTDI board with the service, as run_service() does."""
    config_dir, _ = prepared_configs
    # A product string no catalog entry claims: FtdiBoard falls back to the
    # bundled default config.
    patch_usb_find(
        make_usb_device(
            debugboard.Board.ID_VENDOR_FTDI,
            debugboard.Board.ID_PRODUCT_FTDI,
            SERIAL,
            "anything",
        )
    )
    b = debugboard.Board.create_board(SERIAL, config_dir)
    assert isinstance(b, debugboard.FtdiBoard)

    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setitem(service.boards, SERIAL, b)
    monkeypatch.setitem(service.executors, SERIAL, executor)
    yield b
    executor.shutdown(wait=True)


@pytest.fixture
def client():
    return service.app.test_client()


def pin_for_command(board, command):
    """Return ``(pin_id, pin)`` of the pin wired to ``command``."""
    return next((k, p) for k, p in board.pins.items() if p.command == command)


def port_bit(board, pin):
    return bool(board.ports[pin.bus].status & (1 << pin.pin_number))


# --- read-only endpoints ---------------------------------------------------


def test_root_lists_boards(board, client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_json()
    assert list(body) == [SERIAL]
    assert set(body[SERIAL]["pins"]) == set(board.pins)


def test_root_with_no_boards(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.get_json() == {}


def test_board_view(board, client):
    r = client.get(f"/{SERIAL}")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body["pins"]) == set(board.pins)
    assert set(body["ports"]) == set(board.ports)


@pytest.mark.parametrize(
    "path, attr",
    [
        ("pin", "pins"),
        ("port", "ports"),
        ("quick", "quick_methods"),
        ("command", "commands"),
    ],
)
def test_collections(board, client, path, attr):
    r = client.get(f"/{SERIAL}/{path}")
    assert r.status_code == 200
    assert set(r.get_json()) == set(getattr(board, attr))


def test_single_items(board, client):
    pin_id, pin = next(iter(board.pins.items()))
    assert client.get(f"/{SERIAL}/pin/{pin_id}").get_json() == {
        **pin,
        "port": dict(pin.port),
    }
    assert client.get(f"/{SERIAL}/port/C").get_json() == {"serial": SERIAL, "bus": "C"}
    assert client.get(f"/{SERIAL}/quick/powerOn").get_json() == {
        "name": "powerOn",
        "board": SERIAL,
    }
    assert client.get(f"/{SERIAL}/command/usb0").get_json() == "usb0"


@pytest.mark.parametrize(
    "path",
    [
        "/nope",
        "/nope/pin",
        "/nope/pin/C0",
        "/nope/port",
        "/nope/quick",
        "/nope/command",
        f"/{SERIAL}/pin/Z9",
        f"/{SERIAL}/port/Z",
        f"/{SERIAL}/quick/nope",
        f"/{SERIAL}/command/nope",
    ],
)
def test_get_unknown_returns_empty(board, client, path):
    """GETs of unknown boards and items answer 200 with an empty object."""
    r = client.get(path)
    assert r.status_code == 200
    assert r.get_json() == {}


def test_no_swagger_endpoints(board, client):
    """flask-restx's own routes stay disabled: /swagger.json is just an
    unknown board, and the Swagger UI is not served."""
    r = client.get("/swagger.json")
    assert r.status_code == 200
    assert r.get_json() == {}
    rules = {rule.rule for rule in service.app.url_map.iter_rules()}
    assert "/swagger.json" not in rules
    assert "/" in rules


# --- pins -------------------------------------------------------------------


@pytest.mark.parametrize(
    "send",
    [
        lambda c, url, v: c.put(url, json={"value": v}),
        lambda c, url, v: c.put(url, data={"value": str(v)}),
        lambda c, url, v: c.put(f"{url}?value={v}"),
    ],
    ids=["json", "form", "query"],
)
def test_put_pin(board, client, send):
    pin_id, pin = pin_for_command(board, "usb0")
    url = f"/{SERIAL}/pin/{pin_id}"

    r = send(client, url, 1)
    assert r.status_code == 200
    assert r.get_json()["value"] == 1
    assert pin.value == 1
    assert port_bit(board, pin)

    r = send(client, url, 0)
    assert r.status_code == 200
    assert r.get_json()["value"] == 0
    assert not port_bit(board, pin)


def test_put_pin_reaches_gpio(board, client):
    pin_id, pin = pin_for_command(board, "usb0")
    port = board.ports[pin.bus]
    client.put(f"/{SERIAL}/pin/{pin_id}", json={"value": 1})
    port.gpio.write.assert_called_with(port.status)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"json": {}}, {"json": {"other": 1}}],
    ids=["no-body", "empty-json", "no-value"],
)
def test_put_pin_without_value(board, client, kwargs):
    r = client.put(f"/{SERIAL}/pin/C0", **kwargs)
    assert r.status_code == 400
    assert r.get_json()["message"] == "insufficient parameters"


def test_put_pin_with_non_integer_value(board, client):
    pin = board.pins["C0"]
    before = pin.value
    r = client.put(f"/{SERIAL}/pin/C0", json={"value": "high"})
    assert r.status_code == 400
    assert "value" in r.get_json()["errors"]
    assert pin.value == before


def test_put_unknown_pin(board, client):
    r = client.put(f"/{SERIAL}/pin/Z9", json={"value": 1})
    assert r.status_code == 404
    assert "unknown pin Z9" in r.get_json()["message"]


def test_put_pin_on_unknown_board(board, client):
    r = client.put("/nope/pin/C0", json={"value": 1})
    assert r.status_code == 404
    assert "unknown board nope" in r.get_json()["message"]


# --- commands ---------------------------------------------------------------


def test_put_command(board, client):
    pin_id, pin = pin_for_command(board, "usb0")
    r = client.put(f"/{SERIAL}/command/usb0", json={"value": 1})
    assert r.status_code == 200
    body = r.get_json()
    # The response is a snapshot of every pin on the board.
    assert set(body) == set(board.pins)
    assert body[pin_id]["value"] == 1
    assert pin.value == 1
    assert port_bit(board, pin)


def test_put_command_without_value(board, client):
    r = client.put(f"/{SERIAL}/command/usb0")
    assert r.status_code == 400


@pytest.mark.parametrize("name", ["nope", "__init__", "close", "powerOn"])
def test_put_command_only_dispatches_registered_commands(
    board, client, monkeypatch, name
):
    """The URL must not reach arbitrary Board attributes, quick methods
    included."""
    called = []
    if hasattr(board, name):
        monkeypatch.setattr(board, name, lambda *a: called.append(a))
    r = client.put(f"/{SERIAL}/command/{name}", json={"value": 1})
    assert r.status_code == 404
    assert f"unknown command {name}" in r.get_json()["message"]
    assert called == []


def test_put_command_on_unknown_board(board, client):
    r = client.put("/nope/command/usb0", json={"value": 1})
    assert r.status_code == 404


# --- quick methods ----------------------------------------------------------


def test_put_quick_method(board, client, monkeypatch):
    called = []
    monkeypatch.setattr(board, "powerOn", lambda: called.append(True))
    r = client.put(f"/{SERIAL}/quick/powerOn")
    assert r.status_code == 200
    assert called == [True]
    assert set(r.get_json()) == set(board.pins)


def test_put_quick_method_runs_the_config_script(board, client):
    """powerOn from the bundled config drives real pins; the response shows
    the board state after it ran."""
    r = client.put(f"/{SERIAL}/quick/powerOn")
    assert r.status_code == 200
    body = r.get_json()
    assert body == {k: {**p, "port": dict(p.port)} for k, p in board.pins.items()}


@pytest.mark.parametrize("name", ["nope", "__init__", "usb0"])
def test_put_quick_method_only_dispatches_registered_methods(
    board, client, monkeypatch, name
):
    called = []
    if hasattr(board, name):
        monkeypatch.setattr(board, name, lambda *a: called.append(a))
    r = client.put(f"/{SERIAL}/quick/{name}")
    assert r.status_code == 404
    assert f"unknown quick method {name}" in r.get_json()["message"]
    assert called == []


def test_put_quick_method_failure(board, client, monkeypatch):
    def boom():
        raise RuntimeError("hardware fell over")

    monkeypatch.setattr(board, "powerOn", boom)
    r = client.put(f"/{SERIAL}/quick/powerOn")
    assert r.status_code == 500


def test_put_quick_method_on_unknown_board(board, client):
    r = client.put("/nope/quick/powerOn")
    assert r.status_code == 404


# --- serialisation ----------------------------------------------------------


def test_requests_for_a_board_run_on_its_executor(board, client, monkeypatch):
    """Board operations are queued on the board's own worker thread."""
    threads = []
    monkeypatch.setattr(
        board, "powerOn", lambda: threads.append(threading.current_thread())
    )
    client.put(f"/{SERIAL}/quick/powerOn")
    client.put(f"/{SERIAL}/quick/powerOn")
    assert len(threads) == 2
    assert threads[0] is threads[1]
    assert threads[0] is not threading.current_thread()
