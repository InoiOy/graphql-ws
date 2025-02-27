import json
from collections import OrderedDict

from graphql import graphql

from .constants import (
    GRAPHQL_WS,
    TRANSPORT_WS_PROTOCOL,
    GQL_COMPLETE,
    GQL_CONNECTION_ERROR,
    GQL_CONNECTION_INIT,
    GQL_CONNECTION_TERMINATE,
    GQL_DATA,
    GQL_ERROR,
    GQL_NEXT,
    GQL_START,
    GQL_STOP,
    GQL_SUBSCRIBE,
    GQL_PING,
    GQL_PONG,
)


class ConnectionClosedException(Exception):
    pass


class BaseConnectionContext(object):
    sub_protocol = False

    def __init__(self, ws, request_context=None):
        self.ws = ws
        self.operations = {}
        self.request_context = request_context

        self.sub_protocol = None
        if request_context:
            self.sub_protocol = request_context.get("sub_protocol")

    def has_operation(self, op_id):
        return op_id in self.operations

    def register_operation(self, op_id, async_iterator):
        self.operations[op_id] = async_iterator

    def get_operation(self, op_id):
        return self.operations[op_id]

    def remove_operation(self, op_id):
        try:
            return self.operations.pop(op_id)
        except KeyError:
            return

    def unsubscribe(self, op_id):
        async_iterator = self.remove_operation(op_id)
        if hasattr(async_iterator, "dispose"):
            async_iterator.dispose()
        return async_iterator

    def unsubscribe_all(self):
        for op_id in list(self.operations):
            self.unsubscribe(op_id)

    def receive(self):
        raise NotImplementedError("receive method not implemented")

    def send(self, data):
        raise NotImplementedError("send method not implemented")

    @property
    def closed(self):
        raise NotImplementedError("closed property not implemented")

    def close(self, code, message=''):
        raise NotImplementedError("close method not implemented")


class BaseSubscriptionServer(object):

    def __init__(self, schema, keep_alive=True):
        self.schema = schema
        self.keep_alive = keep_alive

        self.pong_received = None

    def execute(self, params):
        return graphql(self.schema, **dict(params, allow_subscriptions=True))

    def _parse_apollo_message(self, connection_context, op_id, op_type, payload):
        """Parse message for Apollo graphql-ws subprotocol."""
        if op_type == GQL_CONNECTION_INIT:
            return self.on_connection_init(connection_context, op_id, payload)

        elif op_type == GQL_CONNECTION_TERMINATE:
            return self.on_connection_terminate(connection_context, op_id)

        elif op_type == GQL_START:
            assert isinstance(payload, dict), "The payload must be a dict"
            params = self.get_graphql_params(connection_context, payload)
            return self.on_start(connection_context, op_id, params)

        elif op_type == GQL_STOP:
            return self.on_stop(connection_context, op_id)

        return self.send_error(
            connection_context,
            op_id,
            Exception("Invalid message type: {}.".format(op_type)),
        )

    def _parse_transport_ws_message(self, connection_context, op_id, op_type, payload):
        """Parse message for graphql-transport-ws subprotocol."""
        if op_type == GQL_CONNECTION_INIT:
            return self.on_connection_init(connection_context, op_id, payload)

        elif op_type == GQL_SUBSCRIBE:
            assert isinstance(payload, dict), "The payload must be a dict"
            params = self.get_graphql_params(connection_context, payload)
            return self.on_start(connection_context, op_id, params)

        elif op_type == GQL_COMPLETE:
            return self.on_stop(connection_context, op_id)

        elif op_type == GQL_PING:
            return self.send_message(connection_context, op_type=GQL_PONG)

        elif op_type == GQL_PONG:
            if self.pong_received is not None:
                self.pong_received.set()
                return

        return self.send_error(
            connection_context,
            op_id,
            Exception("Invalid message type: {}.".format(op_type)),
        )

    def process_message(self, connection_context, parsed_message):
        op_id = parsed_message.get("id")
        op_type = parsed_message.get("type")
        payload = parsed_message.get("payload")

        if connection_context.sub_protocol == TRANSPORT_WS_PROTOCOL:
            return self._parse_transport_ws_message(connection_context, op_id, op_type, payload)

        return self._parse_apollo_message(connection_context, op_id, op_type, payload)

    def on_connection_init(self, connection_context, op_id, payload):
        raise NotImplementedError("on_connection_init method not implemented")

    def on_connection_terminate(self, connection_context, op_id):
        return connection_context.close(1011)

    def get_graphql_params(self, connection_context, payload):
        context = payload.get("context", connection_context.request_context)
        return {
            "request_string": payload.get("query"),
            "variable_values": payload.get("variables"),
            "operation_name": payload.get("operationName"),
            "context_value": context,
        }

    def on_open(self, connection_context):
        raise NotImplementedError("on_open method not implemented")

    def on_stop(self, connection_context, op_id):
        return connection_context.unsubscribe(op_id)

    def on_close(self, connection_context):
        return connection_context.unsubscribe_all()

    def send_message(self, connection_context, op_id=None, op_type=None, payload=None):
        if op_id is None or connection_context.has_operation(op_id):
            message = self.build_message(op_id, op_type, payload)
            return connection_context.send(message)

    def build_message(self, id, op_type, payload):
        message = {}
        if id is not None:
            message["id"] = id
        if op_type is not None:
            message["type"] = op_type
        if payload is not None:
            message["payload"] = payload
        assert message, "You need to send at least one thing"
        return message

    def send_execution_result(self, connection_context, op_id, execution_result):
        result = self.execution_result_to_dict(execution_result)
        return self.send_message(
            connection_context,
            op_id,
            GQL_DATA if connection_context.sub_protocol == GRAPHQL_WS else GQL_NEXT,
            result,
        )

    def execution_result_to_dict(self, execution_result):
        result = OrderedDict()
        if execution_result.data:
            result["data"] = execution_result.data
        if execution_result.errors:
            result["errors"] = [
                error.formatted for error in execution_result.errors
            ]
        return result

    def send_error(self, connection_context, op_id, error, error_type=None):
        if error_type is None:
            error_type = GQL_ERROR

        assert error_type in [GQL_CONNECTION_ERROR, GQL_ERROR], (
            "error_type should be one of the allowed error messages"
            " GQL_CONNECTION_ERROR or GQL_ERROR"
        )

        error_payload = {"message": str(error)}

        return self.send_message(connection_context, op_id, error_type, error_payload)

    def on_message(self, connection_context, message):
        try:
            if not isinstance(message, dict):
                message = message.replace("\n", " ")
                parsed_message = json.loads(message)
                assert isinstance(parsed_message, dict), "Payload must be an object."
            else:
                parsed_message = message
        except Exception as e:
            return self.send_error(connection_context, None, e)

        return self.process_message(connection_context, parsed_message)
