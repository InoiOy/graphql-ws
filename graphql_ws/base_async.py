import asyncio
import inspect
from abc import ABC, abstractmethod
from types import CoroutineType, GeneratorType
from typing import Any, Dict, List, Union
from weakref import WeakSet
from promise import Promise

from graphql import subscribe, execute, parse

from graphql_ws import base

from .constants import (
    GRAPHQL_WS,
    TRANSPORT_WS_PROTOCOL,
    GQL_COMPLETE,
    GQL_CONNECTION_ACK,
    GQL_CONNECTION_ERROR,
    GQL_PING,
)


CO_ITERABLE_COROUTINE = inspect.CO_ITERABLE_COROUTINE
KEEP_ALIVE_INTERVAL = 2


# Copied from graphql-core v3.1.0 (graphql/pyutils/is_awaitable.py)
def is_awaitable(value: Any) -> bool:
    """Return true if object can be passed to an ``await`` expression.
    Instead of testing if the object is an instance of abc.Awaitable, it checks
    the existence of an `__await__` attribute. This is much faster.
    """
    return (
        # check for coroutine objects
        isinstance(value, CoroutineType)
        # check for old-style generator based coroutine objects
        or isinstance(value, GeneratorType)
        and bool(value.gi_code.co_flags & CO_ITERABLE_COROUTINE)
        # check for other awaitables (e.g. futures)
        or hasattr(value, "__await__")
    )


async def resolve(
    data: Any, _container: Union[List, Dict] = None, _key: Union[str, int] = None
) -> None:
    """
    Recursively wait on any awaitable children of a data element and resolve any
    Promises.
    """
    if is_awaitable(data):
        data = await data
        if isinstance(data, Promise):
            data = data.value  # type: Any
        if _container is not None:
            _container[_key] = data
    if isinstance(data, dict):
        items = data.items()
    elif isinstance(data, list):
        items = enumerate(data)
    else:
        items = None
    if items is not None:
        children = [
            asyncio.ensure_future(resolve(child, _container=data, _key=key))
            for key, child in items
        ]
        if children:
            await asyncio.wait(children)


class BaseAsyncConnectionContext(base.BaseConnectionContext, ABC):
    def __init__(self, ws, request_context=None):
        super().__init__(ws, request_context=request_context)
        self.pending_tasks = WeakSet()

    @abstractmethod
    async def receive(self):
        raise NotImplementedError("receive method not implemented")

    @abstractmethod
    async def send(self, data):
        ...

    @property
    @abstractmethod
    def closed(self):
        ...

    async def close(self, code, message=''):
        await self.ws.close(code=code, message=message.encode())

    def remember_task(self, task):
        self.pending_tasks.add(task)
        # Clear completed tasks
        self.pending_tasks -= WeakSet(
            task for task in self.pending_tasks if task.done()
        )

    async def unsubscribe(self, op_id):
        async_iterator = super().unsubscribe(op_id)
        if getattr(async_iterator, "future", None) and async_iterator.future.cancel():
            await async_iterator.future

        if hasattr(async_iterator, 'aclose'):
            await async_iterator.aclose()

    async def unsubscribe_all(self):
        awaitables = [self.unsubscribe(op_id) for op_id in list(self.operations)]
        for task in self.pending_tasks:
            task.cancel()
            awaitables.append(task)
        if awaitables:
            try:
                await asyncio.gather(*awaitables)
            except asyncio.CancelledError:
                pass


class BaseAsyncSubscriptionServer(base.BaseSubscriptionServer, ABC):

    def __init__(self, schema, keep_alive=True, ping_interval=None, pong_timeout=None, loop=None):
        """
        :param ping_interval: Delay in seconds between pings sent by the server to
            the client for the graphql-ws protocol. None (by default) means that
            we don't send pings.
        :param pong_timeout: Delay in seconds to receive a pong from the client
            after we sent a ping (only for the graphql-ws protocol).
            By default equal to half of the ping_interval.
        """
        super().__init__(schema, keep_alive)

        self.ping_interval = ping_interval
        self.pong_timeout = pong_timeout
        self.loop = loop

        if ping_interval is not None and pong_timeout is None:
            self.pong_timeout = ping_interval / 2

        self.pong_received = asyncio.Event()

    @abstractmethod
    async def handle(self, ws, request_context=None):
        ...

    def process_message(self, connection_context, parsed_message):
        response = super().process_message(connection_context, parsed_message)
        if response:
            task = asyncio.ensure_future(response, loop=self.loop)
            connection_context.remember_task(task)
            return task

    def send_error(self, connection_context, op_id, error, error_type=None):
        task = asyncio.ensure_future(
            super().send_error(connection_context, op_id, error, error_type), loop=self.loop
        )
        connection_context.remember_task(task)
        return task

    async def on_open(self, connection_context):
        pass

    async def on_connect(self, connection_context, payload):
        pass

    async def on_connection_init(self, connection_context, op_id, payload):
        try:
            await self.on_connect(connection_context, payload)
            await self.send_message(connection_context, op_type=GQL_CONNECTION_ACK)

            if connection_context.sub_protocol == TRANSPORT_WS_PROTOCOL and self.ping_interval:
                self.start_ping_task(connection_context)
        except Exception as e:
            if connection_context.sub_protocol == GRAPHQL_WS:
                await self.send_error(connection_context, op_id, e, GQL_CONNECTION_ERROR)
            await connection_context.close(1011)

    async def on_start(self, connection_context, op_id, params):
        # Attempt to unsubscribe first in case we already have a subscription
        # with this id.
        await connection_context.unsubscribe(op_id)

        request_string = params.pop("request_string")
        query = parse(request_string)

        if request_string.startswith("subscription"):
            result = await subscribe(self.schema, query, **params)
        else:
            result = execute(self.schema, query, **params)

        connection_context.register_operation(op_id, result)
        if hasattr(result, "__aiter__"):
            try:
                async for single_result in result:
                    if not connection_context.has_operation(op_id):
                        break
                    await self.send_execution_result(
                        connection_context, op_id, single_result
                    )
            except Exception as e:
                await self.send_error(connection_context, op_id, e)
        else:
            try:
                if is_awaitable(result):
                    result = await result

                await self.send_execution_result(
                    connection_context, op_id, result
                )
            except Exception as e:
                await self.send_error(connection_context, op_id, e)

        await self.send_message(connection_context, op_id, GQL_COMPLETE)
        await connection_context.unsubscribe(op_id)
        await self.on_operation_complete(connection_context, op_id)

    async def send_message(
        self, connection_context, op_id=None, op_type=None, payload=None
    ):
        if op_id is None or connection_context.has_operation(op_id):
            message = self.build_message(op_id, op_type, payload)
            return await connection_context.send(message)

    async def on_operation_complete(self, connection_context, op_id):
        pass

    async def send_execution_result(self, connection_context, op_id, execution_result):
        # Resolve any pending promises
        await resolve(execution_result.data)
        await super().send_execution_result(connection_context, op_id, execution_result)

    async def send_ping_task(self, connection_context):
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                await self.send_message(connection_context, op_type=GQL_PING)

                await asyncio.wait_for(self.pong_received.wait(), self.pong_timeout)

                # Reset for the next iteration
                self.pong_received.clear()
        except asyncio.TimeoutError:
            # No pong received in the appriopriate time, close
            await connection_context.close(
                1011,
                f"No pong received after {self.pong_timeout!r} seconds"
            )

    def start_ping_task(self, connection_context):
        task = asyncio.ensure_future(
            self.send_ping_task(connection_context), loop=self.loop
        )
        connection_context.remember_task(task)
