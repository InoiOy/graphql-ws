import asyncio
import sys
from typing import Awaitable, Callable

import pytest
from aiohttp import WSMsgType
from aiohttp.client import ClientWebSocketResponse
from aiohttp.test_utils import TestClient
from aiohttp.web import Application, WebSocketResponse
from graphql import GraphQLSchema, build_schema
from graphql_ws.aiohttp import AiohttpSubscriptionServer

if sys.version_info >= (3, 8):
    from unittest.mock import AsyncMock
else:
    from asyncmock import AsyncMock


AiohttpClientFactory = Callable[[Application], Awaitable[TestClient]]


def schema() -> GraphQLSchema:
    spec = """
    type Query {
        getMessage: String
    }
    type Mutation {
        setMessage(var: String): String
    }
    type MessageResponse {
        message: String
    }
    type Subscription {
        messages: [MessageResponse!]
        error: String
    }
    schema {
        query: Query
        mutation: Mutation
        subscription: Subscription
    }
    """

    async def messages_subscribe(root, _info):
        await asyncio.sleep(0.1)
        yield [{"message": "foo"}]
        await asyncio.sleep(0.1)
        yield [{"message": "bar"}]

    async def error_subscribe(root, _info):
        raise RuntimeError("baz")

    def dummy(root, _info, **kwargs):
        return "dummy response"

    schema = build_schema(spec)
    schema.subscription_type.fields["messages"].subscribe = messages_subscribe
    schema.subscription_type.fields["messages"].resolve = lambda evt, _info: evt
    schema.subscription_type.fields["error"].subscribe = error_subscribe
    schema.subscription_type.fields["error"].resolve = lambda evt, _info: evt
    schema.query_type.fields["getMessage"].resolve = dummy
    schema.mutation_type.fields["setMessage"].resolve = dummy

    return schema


@pytest.fixture
def client(
    event_loop: asyncio.AbstractEventLoop, aiohttp_client: AiohttpClientFactory
) -> TestClient:
    subscription_server = AiohttpSubscriptionServer(schema())

    async def subscriptions(request):
        conn = WebSocketResponse(protocols=('graphql-ws',))
        await conn.prepare(request)
        await subscription_server.handle(conn)
        return conn

    app = Application()
    app["subscription_server"] = subscription_server
    app.router.add_get('/subscriptions', subscriptions)
    return event_loop.run_until_complete(aiohttp_client(app))


@pytest.fixture
async def connection(client: TestClient) -> ClientWebSocketResponse:
    conn = await client.ws_connect("/subscriptions")
    yield conn
    await conn.close()


async def test_connection_closed_on_error(connection: ClientWebSocketResponse):
    connection._writer.transport.write(b'0' * 500)
    response = await connection.receive()
    assert response.type == WSMsgType.CLOSE


async def test_connection_init(connection: ClientWebSocketResponse):
    await connection.send_str('{"type":"connection_init","payload":{}}')
    response = await connection.receive()
    assert response.type == WSMsgType.TEXT
    assert response.data == '{"type": "connection_ack"}'


async def test_connection_init_rejected_on_error(
    monkeypatch, client: TestClient, connection: ClientWebSocketResponse
):
    # raise exception in AiohttpSubscriptionServer.on_connect
    monkeypatch.setattr(
        client.app["subscription_server"],
        "on_connect",
        AsyncMock(side_effect=RuntimeError()),
    )
    await connection.send_str('{"type":"connection_init", "payload": {}}')
    response = await connection.receive()
    assert response.type == WSMsgType.TEXT
    assert response.json()['type'] == 'connection_error'


async def test_messages_subscription(connection: ClientWebSocketResponse):
    await connection.send_str('{"type":"connection_init","payload":{}}')
    await connection.receive()
    await connection.send_str(
        '{"id":"1","type":"start","payload":{"query":"subscription MySub { messages \n { message type } }"}}'
    )
    first = await connection.receive_str()
    assert (
        first == '{"id": "1", "type": "data", "payload": {"data": {"messages": [{"message": "foo"}]}}}'
    )
    second = await connection.receive_str()
    assert (
        second == '{"id": "1", "type": "data", "payload": {"data": {"messages": [{"message": "bar"}]}}}'
    )
    resolve_message = await connection.receive_str()
    assert resolve_message == '{"id": "1", "type": "complete"}'


async def test_subscription_resolve_error(connection: ClientWebSocketResponse):
    await connection.send_str('{"type":"connection_init","payload":{}}')
    await connection.receive()
    await connection.send_str(
        '{"id":"2","type":"start","payload":{"query":"subscription MySub { error }"}}'
    )
    error = await connection.receive_json()
    assert error["payload"]["errors"][0]["message"] == "baz"

async def test_messages_query(connection: ClientWebSocketResponse):
    await connection.send_str('{"type":"connection_init","payload":{}}')
    await connection.receive()
    await connection.send_str(
        '{"id":"1","type":"start","payload":{"query":"query MyQuery { getMessage }"}}'
    )
    first = await connection.receive_str()
    assert (
        first == '{"id": "1", "type": "data", "payload": {"data": {"getMessage": "dummy response"}}}'
    )
    resolve_message = await connection.receive_str()
    assert resolve_message == '{"id": "1", "type": "complete"}'

async def test_messages_mutation(connection: ClientWebSocketResponse):
    await connection.send_str('{"type":"connection_init","payload":{}}')
    await connection.receive()
    await connection.send_str(
        '{"id":"1","type":"start","payload":{"query":"mutation MyMutation($message: String) { setMessage(message: $message) }","variables":{"message":"test_value"}}}'
    )
    first = await connection.receive_str()
    assert (
        first == '{"id": "1", "type": "data", "payload": {"data": {"setMessage": "dummy response"}}}'
    )
    resolve_message = await connection.receive_str()
    assert resolve_message == '{"id": "1", "type": "complete"}'
