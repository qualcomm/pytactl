# Copyright (c)i 2025-2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

import logging

from flask import Flask
from flask_restx import Api as RestxApi
from flask_restx import Resource, reqparse
from werkzeug.exceptions import BadRequest
from werkzeug.serving import run_simple

from .debugboard import Board, TACException


class Api(RestxApi):
    """ flask-restx Api without the Swagger UI or its root route

    flask-restx normally registers its own endpoint at "/" which would conflict with BoardList
    This keeps "/" free, as in Flask-RESTful
    """

    def _register_doc(self, app_or_blueprint):
        pass


app = Flask(__name__)
api = Api(app, doc=False)

logger = logging.getLogger()
boards = {}

rparser = reqparse.RequestParser()
rparser.add_argument(
    "value", type=int, help="1 for setting pin HIGH, 0 for setting pin LOW"
)


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
        b = boards.get(boardid, {})
        if b:
            try:
                getattr(b, name)()
            except Exception:
                raise TACException
        return b.pins


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
        b = boards.get(boardid, {})
        if b:
            try:
                c = getattr(b, name)
                if c:
                    c(value)
            except Exception:
                raise TACException
        return b.pins


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
        b = boards.get(boardid, {})
        if b:
            p = b.pins.get(pinid)
            p.set(value)
        return b.pins.get(pinid)


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

    run_simple(hostname, port, app)
