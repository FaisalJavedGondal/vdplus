import os
import asyncio
import aiohttp
from aiohttp import web
import logging
import datetime
import time
import concurrent.futures

from models.user import User
from k8s import create_pod, delete_pod

MSGCODE_AUTH = "6"
LOCAL_GOTTY_INSTANCE = 'http://localhost:8181'
LIVE_GOTTY_PORT = '9000'
THROTTLE = 5
PROTOCOL_FOR_TTY_INPUT = '1'
PROTOCOL_FOR_TTY_PING = '2'

def using_local_gotty():
    if os.getenv("USE_REMOTE_K8S") == "true":
        return False
    if os.getenv("VD_ENV") == "dev" or os.getenv("CI") == "true":
        return True
    return False

async def tty_websocket_handler(request):
    logging.debug("Websocket handler triggered")
    queue = asyncio.Queue()

    # Inbound from the browser
    inbound = web.WebSocketResponse()
    await inbound.prepare(request)
    user = await auth_connection(inbound)
    if user:
        await inbound.send_str(MSGCODE_AUTH + "auth SUCCESS")
    else:
        await inbound.send_str(MSGCODE_AUTH + "auth FAIL")
        return inbound

    try:
        await manage_connection(queue, inbound, user)
    except Exception as e:
        delete_vd_container(user)
        raise e

def delete_vd_container(user):
    if using_local_gotty():
        return
    user.current_pod_ip = ''
    user.save()
    delete_pod(str(user.id))

async def manage_connection(queue, inbound, user):
    logging.debug("Handling incoming websocket for user ID: " + str(user.id))
    inbound_task = asyncio.create_task(inbound_socket(inbound, queue, user))

    vd_container_address = await get_vd_container_address(user)
    if not vd_container_address:
        return inbound

    # Outbound to dedicated VisiData container
    session = aiohttp.ClientSession()
    logging.debug("Attempting to open outbound socket to: " + vd_container_address)
    outbound = await session.ws_connect(vd_container_address + "/ws")
    outbound_task = asyncio.create_task(outbound_socket(outbound, queue, user))

    if user.last_input:
        last_recorded_input = user.last_input.timestamp()
    else:
        last_recorded_input = time.time()

    # This loop is paramount in providing low latency UI feedback.
    # Anything that happens in here should be low on IO blocking and CPU work.
    while True:
        source, data = await queue.get()
        if source == 'inbound':
            last_recorded_input = await idle_watcher(last_recorded_input, data, user)
            await outbound.send_str(data)
        if source == 'outbound':
            idle_killer(data, last_recorded_input, user)
            await inbound.send_str(data)

    inbound_task.cancel()
    outbound_task.cancel()

async def idle_watcher(last_recorded_input, data, user):
    if not data.startswith(PROTOCOL_FOR_TTY_INPUT):
        return last_recorded_input

    timestamp = time.time()
    elapsed = timestamp - last_recorded_input
    if elapsed < THROTTLE:
        return last_recorded_input

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        await loop.run_in_executor(pool, save_input_timestamp, user)
    return timestamp

# Save timestamp to DB because a user could reconnect to a session via a different
# container to this one.
def save_input_timestamp(user):
    logging.debug("Timestamping user's last input record")
    user.last_input = datetime.datetime.now()
    user.save()

def idle_killer(data, last_recorded_input, user):
    if not data.startswith(PROTOCOL_FOR_TTY_PING):
        return
    if time.time() - last_recorded_input > user.idle_timeout:
        message = f"Idle timeout ({user.idle_timeout}), deleting pod for user: {user.id}"
        logging.info(message)
        delete_vd_container(user)

async def get_vd_container_address(user):
    if using_local_gotty():
        # A standard local GoTTY instance
        return LOCAL_GOTTY_INSTANCE
    if user.current_pod_ip != '' and user.current_pod_ip != None:
        url = 'http://' + user.current_pod_ip + ':' + LIVE_GOTTY_PORT
        logging.debug("Reconnecting to user " + str(user.id) + "'s existing pod: " + url)
        return url
    else:
        ip = create_pod(user)
        if ip:
            url = 'http://' + ip + ':' + LIVE_GOTTY_PORT
            logging.debug("Created pod for user " + str(user.id) + " at: " + url)
            user.current_pod_ip = ip
            user.save()
            return url
        else:
            logging.error("Couldn't create container for user: " + str(user.id))
            return False

async def auth_connection(socket):
    async for msg in socket:
        if msg.type == aiohttp.WSMsgType.TEXT:
            user = User.select().where(User.token == msg.data)
            if user.exists():
                return user.get()
            else:
                return False
        elif msg.type == aiohttp.WSMsgType.ERROR:
            logging.error('Inbound webscoket closed with exception %s' % socket.exception())
            return False
    return False

async def inbound_socket(inbound, queue, user: User):
    async for msg in inbound:
        if msg.type == aiohttp.WSMsgType.TEXT:
            if msg.data == 'close':
                await inbound_close(inbound, user)
            else:
                queue.put_nowait(['inbound', msg.data])
        elif msg.type == aiohttp.WSMsgType.ERROR:
            await inbound_close(inbound, user)
            logging.error('Inbound websocket closed with exception %s' % inbound.exception())
    await inbound_close(inbound, user)

async def inbound_close(inbound, user):
    await inbound.close()

async def outbound_socket(outbound, queue, user: User):
    while True:
        msg = await outbound.receive()

        if msg.type == aiohttp.WSMsgType.TEXT:
            if msg.data == 'close':
                await outbound_close(outbound, user)
                break
            else:
                queue.put_nowait(['outbound', msg.data])
        elif msg.type == aiohttp.WSMsgType.CLOSED:
            await outbound_close(outbound, user)
            break
        elif msg.type == aiohttp.WSMsgType.ERROR:
            await outbound_close(outbound, user)
            break

async def outbound_close(outbound, user):
    delete_vd_container(user)
    await outbound.close()
