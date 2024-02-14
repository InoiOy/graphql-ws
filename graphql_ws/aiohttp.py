import json
from asyncio import shield

from aiohttp import WSMsgType

from .base import ConnectionClosedException
from .base_async import BaseAsyncConnectionContext, BaseAsyncSubscriptionServer


class AiohttpConnectionContext(BaseAsyncConnectionContext):
    async def receive(self):
        msg = await self.ws.receive()
        if msg.type == WSMsgType.TEXT:
            return msg.data
        elif msg.type in [WSMsgType.ERROR, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.CLOSE]:
            raise ConnectionClosedException()

    async def send(self, data):
        if self.closed:
            return
        await self.ws.send_str(json.dumps(data))

    @property
    def closed(self):
        return self.ws.closed


class AiohttpSubscriptionServer(BaseAsyncSubscriptionServer):
    async def _handle(self, ws, request_context=None):
        connection_context = AiohttpConnectionContext(ws, request_context)
        await self.on_open(connection_context)
        while True:
            try:
                if connection_context.closed:
                    raise ConnectionClosedException()
                message = await connection_context.receive()
                self.on_message(connection_context, message)
            except ConnectionClosedException:
                break

        await self.on_close(connection_context)

    async def handle(self, ws, request_context=None):
        await shield(self._handle(ws, request_context))
