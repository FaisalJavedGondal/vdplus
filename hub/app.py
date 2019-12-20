import aiohttp
import asyncio
from aiohttp import web

routes = web.RouteTableDef()

@routes.get('/api/auth')
async def auth(request):
    return web.json_response({
        "response": {
            "username": request.query["username"],
            "token": "abc123"
        },
        "meta": {}
    })

async def websocket_handler(request):
    queue = asyncio.Queue()

    # Inbound from the browser
    inbound = web.WebSocketResponse()
    await inbound.prepare(request)
    inbound_task = asyncio.create_task(inbound_socket(inbound, queue))

    # Outbound to GoTTY. Need to schedule container with k8s and get the created
    # container's port.
    session = aiohttp.ClientSession()
    outbound = await session.ws_connect('http://localhost:8181/ws')
    outbound_task = asyncio.create_task(outbound_socket(outbound, queue))

    while True:
        source, data = await queue.get()
        if source == 'inbound':
            await outbound.send_str(data)
        if source == 'outbound':
            await inbound.send_str(data)

    inbound_task.cancel()
    outbound_task.cancel()

async def inbound_socket(inbound, queue):
    async for msg in inbound:
        if msg.type == aiohttp.WSMsgType.TEXT:
            if msg.data == 'close':
                await inbound.close()
            else:
                queue.put_nowait(['inbound', msg.data])
        elif msg.type == aiohttp.WSMsgType.ERROR:
            print('Inbound webscoket closed with exception %s' % inbound.exception())

async def outbound_socket(outbound, queue):
    while True:
        msg = await outbound.receive()

        if msg.type == aiohttp.WSMsgType.TEXT:
            if msg.data == 'close':
                await outbound.close()
                break
            else:
                queue.put_nowait(['outbound', msg.data])
        elif msg.type == aiohttp.WSMsgType.CLOSED:
            break
        elif msg.type == aiohttp.WSMsgType.ERROR:
            break

@routes.get('/{path:.*}')
async def root_handler(request):
    return aiohttp.web.FileResponse('./spa/dist/index.html')

async def create_app():
    app = web.Application()
    app.add_routes([web.get('/ws', websocket_handler)])
    app.router.add_static('/assets/', './spa/dist/')
    app.add_routes(routes)
    return app

if __name__ == '__main__':
    web.run_app(create_app())
