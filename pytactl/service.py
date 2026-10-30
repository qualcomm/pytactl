#!/usr/bin/env python3

# Copyright (c)i 2025-2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

import logging
from concurrent.futures import ThreadPoolExecutor

from flask import Flask
from flask_restful import Api, Resource, reqparse
from werkzeug.exceptions import BadRequest, NotFound
from werkzeug.serving import run_simple

from .debugboard import Board, TACException

app = Flask(__name__)
api = Api(app)

logger = logging.getLogger()
boards = {}
# One single-worker executor per board: requests for the same board are
# executed one at a time in FIFO order, different boards run in parallel.
executors = {}

rparser = reqparse.RequestParser()
rparser.add_argument(
    "value", type=int, help="1 for setting pin HIGH, 0 for setting pin LOW"
)


def get_board(boardid):
    b = boards.get(boardid)
    if b is None:
        raise NotFound(f"unknown board {boardid}")
    return b


def run_on_board(boardid, fn):
    b = get_board(boardid)
    return executors[boardid].submit(fn, b).result()


def snapshot_pins(b):
    # Copy while still on the board worker so the response isn't mutated by
    # the next queued request during serialization.
    return {k: dict(v) for k, v in b.pins.items()}


class BoardView(Resource):
    def get(self, boardid):
        return boards.get(boardid, {})


class BoardList(Resource):
    def get(self):
        return boards


class QuickMethodView(Resource):
    def get(self, boardid, name):
        b = boards.get(boardid, {})
        if b:
            return b.quick_methods.get(name, {})
        return {}

    def put(self, boardid, name):
        def job(b):
            try:
                getattr(b, name)()
            except Exception:
                raise TACException
            return snapshot_pins(b)

        return run_on_board(boardid, job)


class QuickMethodList(Resource):
    def get(self, boardid):
        b = boards.get(boardid, {})
        if b:
            return b.quick_methods
        return {}


class CommandView(Resource):
    def get(self, boardid, name):
        b = boards.get(boardid, {})
        if b:
            return b.commands.get(name, {})
        return {}

    def put(self, boardid, name):
        args = rparser.parse_args()
        value = args.get("value")
        if value is None:
            raise BadRequest("insufficient parameters")

        def job(b):
            try:
                c = getattr(b, name)
                if c:
                    c(value)
            except Exception:
                raise TACException
            return snapshot_pins(b)

        return run_on_board(boardid, job)


class CommandList(Resource):
    def get(self, boardid):
        b = boards.get(boardid, {})
        if b:
            return b.commands
        return {}


class PinView(Resource):
    def get(self, boardid, pinid):
        b = boards.get(boardid, {})
        if b:
            return b.pins.get(pinid, {})
        return {}

    def put(self, boardid, pinid):
        args = rparser.parse_args()
        value = args.get("value")
        if value is None:
            raise BadRequest("insufficient parameters")

        def job(b):
            p = b.pins.get(pinid)
            if p is None:
                raise NotFound(f"unknown pin {pinid}")
            p.set(value)
            return dict(p)

        return run_on_board(boardid, job)


class PinList(Resource):
    def get(self, boardid):
        b = boards.get(boardid, {})
        if b:
            return b.pins
        return {}


class PortView(Resource):
    def get(self, boardid, portid):
        b = boards.get(boardid, {})
        if b:
            return b.ports.get(portid, {})
        return {}


class PortList(Resource):
    def get(self, boardid):
        b = boards.get(boardid, {})
        if b:
            return b.ports
        return {}


api.add_resource(BoardList, "/")
api.add_resource(BoardView, "/<boardid>")
api.add_resource(PinView, "/<boardid>/pin/<pinid>")
api.add_resource(PinList, "/<boardid>/pin")
api.add_resource(QuickMethodView, "/<boardid>/quick/<name>")
api.add_resource(QuickMethodList, "/<boardid>/quick")
api.add_resource(PortView, "/<boardid>/port/<portid>")
api.add_resource(PortList, "/<boardid>/port")
api.add_resource(CommandView, "/<boardid>/command/<name>")
api.add_resource(CommandList, "/<boardid>/command")


def run_service(serials, tac_config_path, hostname="0.0.0.0", port=5000):
    for serial in serials:
        boards.update({serial: Board.create_board(serial, tac_config_path)})
        executors[serial] = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"board-{serial}"
        )

    run_simple(hostname, port, app, threaded=True)
