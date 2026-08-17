import asyncio
import logging
import sys
import time

import socketio
from socketio.exceptions import ConnectionRefusedError as SocketConnectionRefused

from selfai_ui.config import (
    API_KEY_ALLOWED_ENDPOINTS,
    ENABLE_API_KEY,
    ENABLE_API_KEY_ENDPOINT_RESTRICTIONS,
)
from selfai_ui.env import (
    ENABLE_WEBSOCKET_SUPPORT,
    GLOBAL_LOG_LEVEL,
    REDIS_KEY_PREFIX,
    SRC_LOG_LEVELS,
    WEBSOCKET_MANAGER,
    WEBSOCKET_REDIS_URL,
)
from selfai_ui.models.channels import Channels
from selfai_ui.models.chats import Chats
from selfai_ui.models.users import UserNameResponse, Users
from selfai_ui.socket.utils import InMemoryDict, NoOpLock, RedisDict, RedisLock
from selfai_ui.utils.auth import decode_token

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["SOCKET"])


if WEBSOCKET_MANAGER == "redis":
    mgr = socketio.AsyncRedisManager(WEBSOCKET_REDIS_URL)
    sio = socketio.AsyncServer(
        cors_allowed_origins=[],
        async_mode="asgi",
        transports=(["websocket"] if ENABLE_WEBSOCKET_SUPPORT else ["polling"]),
        allow_upgrades=ENABLE_WEBSOCKET_SUPPORT,
        always_connect=True,
        client_manager=mgr,
    )
else:
    sio = socketio.AsyncServer(
        cors_allowed_origins=[],
        async_mode="asgi",
        transports=(["websocket"] if ENABLE_WEBSOCKET_SUPPORT else ["polling"]),
        allow_upgrades=ENABLE_WEBSOCKET_SUPPORT,
        always_connect=True,
    )


# Timeout duration in seconds
TIMEOUT_DURATION = 3

# Dictionary to maintain the user pool

if WEBSOCKET_MANAGER == "redis":
    log.debug("Using Redis to manage websockets.")
    SESSION_POOL = RedisDict(f"{REDIS_KEY_PREFIX}:session_pool", redis_url=WEBSOCKET_REDIS_URL)
    USER_POOL = RedisDict(f"{REDIS_KEY_PREFIX}:user_pool", redis_url=WEBSOCKET_REDIS_URL)
    USAGE_POOL = RedisDict(f"{REDIS_KEY_PREFIX}:usage_pool", redis_url=WEBSOCKET_REDIS_URL)

    clean_up_lock = RedisLock(
        redis_url=WEBSOCKET_REDIS_URL,
        lock_name=f"{REDIS_KEY_PREFIX}:usage_cleanup_lock",
        timeout_secs=TIMEOUT_DURATION * 2,
    )
else:
    SESSION_POOL = InMemoryDict()
    USER_POOL = InMemoryDict()
    USAGE_POOL = InMemoryDict()
    clean_up_lock = NoOpLock()

aquire_func = clean_up_lock.aquire_lock
renew_func = clean_up_lock.renew_lock
release_func = clean_up_lock.release_lock


async def periodic_usage_pool_cleanup():
    if not await aquire_func():
        log.debug("Usage pool cleanup lock already exists. Not running it.")
        return
    log.debug("Running periodic_usage_pool_cleanup")
    try:
        while True:
            if not await renew_func():
                log.error("Unable to renew cleanup lock. Exiting usage pool cleanup.")
                raise Exception("Unable to renew usage pool cleanup lock.")

            now = int(time.time())
            send_usage = False
            for model_id, connections in await USAGE_POOL.aitems():
                # Creating a list of sids to remove if they have timed out
                expired_sids = [
                    sid for sid, details in connections.items() if now - details["updated_at"] > TIMEOUT_DURATION
                ]

                for sid in expired_sids:
                    del connections[sid]

                if not connections:
                    log.debug(f"Cleaning up model {model_id} from usage pool")
                    await USAGE_POOL.adelete(model_id)
                else:
                    await USAGE_POOL.aset(model_id, connections)

                send_usage = True

            if send_usage:
                # Emit updated usage information after cleaning
                await sio.emit("usage", {"models": await get_models_in_use()})

            await asyncio.sleep(TIMEOUT_DURATION)
    finally:
        await release_func()


app = socketio.ASGIApp(
    sio,
    socketio_path="/ws/socket.io",
)


async def get_models_in_use():
    # List models that are currently in use
    models_in_use = await USAGE_POOL.akeys()
    return models_in_use


@sio.on("usage")
async def usage(sid, data):
    model_id = data["model"]
    # Record the timestamp for the last update
    current_time = int(time.time())

    # Store the new usage data and task
    existing = await USAGE_POOL.aget(model_id, {})
    await USAGE_POOL.aset(
        model_id,
        {
            **existing,
            sid: {"updated_at": current_time},
        },
    )

    # Broadcast the usage data to all clients
    await sio.emit("usage", {"models": await get_models_in_use()})


#: The path this Socket.IO server is mounted at (`app.mount("/ws", socket_app)`
#: plus the `socketio_path` above). It is the allowlist entry an operator adds
#: to API_KEY_ALLOWED_ENDPOINTS to permit API-key attach when endpoint
#: restrictions are switched on -- see `resolve_socket_user`.
SOCKET_PATH = "/ws/socket.io"

#: What a caller must send to authenticate, quoted back on refusal.
_CREDENTIAL_HINT = "send auth={'token': '<session jwt>'} or auth={'api_key': 'sk-...'}"


def resolve_socket_user(auth):
    """Resolve the user behind a Socket.IO `auth` payload.

    Attach auth is not inference auth (self.ai#81): a crew captain Pod is cattle
    -- a fresh Pod per session, a dozen-plus identities -- so it cannot depend on
    a human signing in to paste a session JWT. It already carries an `sk-` API
    key for inference, which mint can issue and rotate unattended, so that same
    key is what it should be able to attach with. This accepts either credential:

    - ``auth["api_key"]`` -- what crew-code sends when its `apiKey` field is set
      (remote-attach.ts). Previously ignored outright, which is why the API-key
      path was refused at connect.
    - ``auth["token"]`` -- a session JWT (the browser path, unchanged), or an
      ``sk-`` key, since a caller that only has one credential slot will put it
      there. `decode_token` is a bare `jwt.decode`, so an `sk-` string reaching
      it could only ever fail; routing it to the API-key path instead is strictly
      more useful.

    Returns ``(user, refusal)``. On success ``refusal`` is None; on failure
    ``user`` is None and ``refusal`` is a ``(code, message)`` pair -- a stable
    code to branch on and a sentence a human can act on. A bare rejection is
    indistinguishable from a network fault or a wrong URL, which is most of the
    debugging cost in a fleet.

    API-key attach honours the same two switches the HTTP path does
    (`utils/auth.get_current_user`): ENABLE_API_KEY, and -- because a socket the
    allowlist cannot name would silently widen an operator's deliberate
    narrowing -- API_KEY_ALLOWED_ENDPOINTS, matched against SOCKET_PATH. JWT
    attach is unaffected by both, exactly as on HTTP.
    """
    if not auth:
        return None, ("auth_required", f"no credential supplied: {_CREDENTIAL_HINT}")

    api_key = auth.get("api_key")
    token = auth.get("token")

    if not api_key and isinstance(token, str) and token.startswith("sk-"):
        api_key, token = token, None

    if api_key:
        if not ENABLE_API_KEY.value:
            return None, (
                "api_key_disabled",
                "api key authentication is disabled on this server (ENABLE_API_KEY=False)",
            )

        if ENABLE_API_KEY_ENDPOINT_RESTRICTIONS.value:
            allowed = [path.strip() for path in str(API_KEY_ALLOWED_ENDPOINTS.value).split(",")]
            if SOCKET_PATH not in allowed:
                return None, (
                    "api_key_endpoint_restricted",
                    f"api key authentication is not permitted for {SOCKET_PATH}; "
                    f"add it to API_KEY_ALLOWED_ENDPOINTS to allow socket attach",
                )

        user = Users.get_user_by_api_key(api_key)
        if user is None:
            return None, ("invalid_api_key", "api key is not recognised")

        Users.update_user_last_active_by_id(user.id)
        return user, None

    if not token:
        return None, ("auth_required", f"no credential supplied: {_CREDENTIAL_HINT}")

    data = decode_token(token)
    if data is None or "id" not in data:
        return None, ("invalid_token", "session token is invalid or has expired")

    user = Users.get_user_by_id(data["id"])
    if user is None:
        return None, ("unknown_user", "token is well-formed but names no known user")

    return user, None


@sio.event
async def connect(sid, environ, auth):
    user, refusal = resolve_socket_user(auth)

    # Refuse a connection that carries no valid credential. Raising socket.io's
    # ConnectionRefusedError is the documented rejection and carries the reason:
    # with always_connect=True the server sends CONNECT then an immediate
    # DISCONNECT whose payload is the exception's error_args, so the client is
    # told *why* rather than seeing a bare drop. An anonymous or invalid-
    # credential client never reaches any event handler -- core's or a mod's. A
    # mod that registers a namespace on this server therefore inherits this gate
    # for free; that inheritance is the reason mods do not mount their own
    # websocket stacks (see the mods contract, Decision 2).
    if user is None:
        code, message = refusal
        log.info("socket connect refused (sid=%s): %s -- %s", sid, code, message)
        raise SocketConnectionRefused(message, {"code": code})

    await SESSION_POOL.aset(sid, user.model_dump())
    existing_sids = await USER_POOL.aget(user.id)
    if existing_sids:
        await USER_POOL.aset(user.id, existing_sids + [sid])
    else:
        await USER_POOL.aset(user.id, [sid])

    # print(f"user {user.name}({user.id}) connected with session ID {sid}")
    await sio.emit("user-list", {"user_ids": await USER_POOL.akeys()})
    await sio.emit("usage", {"models": await get_models_in_use()})
    return True


@sio.on("user-join")
async def user_join(sid, data):

    auth = data["auth"] if "auth" in data else None
    user, refusal = resolve_socket_user(auth)
    if user is None:
        # An event handler has no refusal packet to raise, and the ack shape is
        # the client's contract, so this stays a silent return -- but the reason
        # is logged rather than lost.
        log.info("socket user-join refused (sid=%s): %s -- %s", sid, *refusal)
        return

    await SESSION_POOL.aset(sid, user.model_dump())
    existing_sids = await USER_POOL.aget(user.id)
    if existing_sids:
        await USER_POOL.aset(user.id, existing_sids + [sid])
    else:
        await USER_POOL.aset(user.id, [sid])

    # Join all the channels
    channels = Channels.get_channels_by_user_id(user.id)
    log.debug(f"{channels=}")
    for channel in channels:
        await sio.enter_room(sid, f"channel:{channel.id}")

    # print(f"user {user.name}({user.id}) connected with session ID {sid}")

    await sio.emit("user-list", {"user_ids": await USER_POOL.akeys()})
    return {"id": user.id, "name": user.name}


@sio.on("join-channels")
async def join_channel(sid, data):
    auth = data["auth"] if "auth" in data else None
    user, refusal = resolve_socket_user(auth)
    if user is None:
        log.info("socket join-channels refused (sid=%s): %s -- %s", sid, *refusal)
        return

    # Join all the channels
    channels = Channels.get_channels_by_user_id(user.id)
    log.debug(f"{channels=}")
    for channel in channels:
        await sio.enter_room(sid, f"channel:{channel.id}")


@sio.on("channel-events")
async def channel_events(sid, data):
    room = f"channel:{data['channel_id']}"
    participants = sio.manager.get_participants(
        namespace="/",
        room=room,
    )

    sids = [sid for sid, _ in participants]
    if sid not in sids:
        return

    event_data = data["data"]
    event_type = event_data["type"]

    if event_type == "typing":
        session_user = await SESSION_POOL.aget(sid)
        await sio.emit(
            "channel-events",
            {
                "channel_id": data["channel_id"],
                "message_id": data.get("message_id", None),
                "data": event_data,
                "user": UserNameResponse(**session_user).model_dump(),
            },
            room=room,
        )


@sio.on("user-list")
async def user_list(sid):
    await sio.emit("user-list", {"user_ids": await USER_POOL.akeys()})


@sio.event
async def disconnect(sid):
    user = await SESSION_POOL.aget(sid)
    if user is not None:
        await SESSION_POOL.adelete(sid)

        user_id = user["id"]
        remaining_sids = [_sid for _sid in await USER_POOL.aget(user_id, []) if _sid != sid]

        if len(remaining_sids) == 0:
            await USER_POOL.adelete(user_id)
        else:
            await USER_POOL.aset(user_id, remaining_sids)

        await sio.emit("user-list", {"user_ids": await USER_POOL.akeys()})
    else:
        pass
        # print(f"Unknown session ID {sid} disconnected")


def get_event_emitter(request_info):
    async def __event_emitter__(event_data):
        user_id = request_info["user_id"]
        user_sids = await USER_POOL.aget(user_id, [])
        session_ids = list(set(user_sids + [request_info["session_id"]]))

        for session_id in session_ids:
            await sio.emit(
                "chat-events",
                {
                    "chat_id": request_info["chat_id"],
                    "message_id": request_info["message_id"],
                    "data": event_data,
                },
                to=session_id,
            )

        if "type" in event_data and event_data["type"] == "status":
            Chats.add_message_status_to_chat_by_id_and_message_id(
                request_info["chat_id"],
                request_info["message_id"],
                event_data.get("data", {}),
            )

        if "type" in event_data and event_data["type"] == "message":
            message = Chats.get_message_by_id_and_message_id(
                request_info["chat_id"],
                request_info["message_id"],
            )

            content = message.get("content", "")
            content += event_data.get("data", {}).get("content", "")

            Chats.upsert_message_to_chat_by_id_and_message_id(
                request_info["chat_id"],
                request_info["message_id"],
                {
                    "content": content,
                },
            )

        if "type" in event_data and event_data["type"] == "replace":
            content = event_data.get("data", {}).get("content", "")

            Chats.upsert_message_to_chat_by_id_and_message_id(
                request_info["chat_id"],
                request_info["message_id"],
                {
                    "content": content,
                },
            )

    return __event_emitter__


def get_event_call(request_info):
    async def __event_call__(event_data):
        response = await sio.call(
            "chat-events",
            {
                "chat_id": request_info["chat_id"],
                "message_id": request_info["message_id"],
                "data": event_data,
            },
            to=request_info["session_id"],
        )
        return response

    return __event_call__


async def get_user_id_from_session_pool(sid):
    user = await SESSION_POOL.aget(sid)
    if user:
        return user["id"]
    return None


async def get_user_ids_from_room(room):
    active_session_ids = sio.manager.get_participants(
        namespace="/",
        room=room,
    )

    active_user_ids = set()
    for session_id in active_session_ids:
        user = await SESSION_POOL.aget(session_id[0])
        if user:
            active_user_ids.add(user["id"])
    return list(active_user_ids)


async def get_active_status_by_user_id(user_id):
    return await USER_POOL.acontains(user_id)


async def emit_to_user(user_id, event, data, *, namespace=None):
    """Emit an event to every active session of one user.

    This is the piece a mod needs and could not previously reach without an
    internal import: the user-to-session mapping lives in USER_POOL, and
    get_event_emitter() is chat-specific (keyed by chat/message id, writes to
    Chats). A reconciler-watch loop reacting to external state has no request,
    no chat, and no sid -- only a user id. This gives it a supported way in.

    - Callable from outside any request context; needs only the user id.
    - A user with no active session is a no-op, not an error: their session
      list is absent and there is simply nothing to emit to.
    - Under the Redis manager the emit fans out across replicas, so a user
      connected to a different replica still receives it -- the same mechanism
      core's own emits already ride.
    - `namespace` targets a mod's registered namespace; omitted, it uses the
      default namespace.

    Exposed to mods through the facade as `emit_to_user`.
    """
    session_ids = await USER_POOL.aget(user_id)
    if not session_ids:
        return
    for session_id in session_ids:
        await sio.emit(event, data, to=session_id, namespace=namespace)


def install_namespace_auth(namespace: str) -> None:
    """Install core's connect-auth gate on a mod's Socket.IO namespace.

    A namespace with no `connect` handler is accepted UNAUTHENTICATED: python-
    socketio's `_handle_connect` treats "no handler" as not-refused, so the
    default namespace's gate does NOT extend to a mod's namespace. Without this,
    the contract's "a mod namespace inherits core's auth for free" is false and
    any Engine.IO client could connect to a mod namespace the operator believed
    was gated.

    This registers a `connect` handler on `namespace` that runs the same
    credential resolution as the default namespace (`resolve_socket_user` --
    session JWT or `sk-` API key) and refuses an anonymous or invalid-credential
    client with the same coded reason, so the mod's namespace behaves identically
    to core's: a mod gains API-key attach for free, and cannot drift. The
    authenticated sid is recorded into SESSION_POOL/USER_POOL under the same
    keys the default namespace uses, so `emit_to_user(namespace=...)` and a mod
    handler's `get_user_id_from_session_pool(sid)` both see the identity core
    resolved -- the mod never decodes a token itself.
    """

    async def _connect(sid, environ, auth):
        user, refusal = resolve_socket_user(auth)
        if user is None:
            code, message = refusal
            log.info("socket connect refused on %s (sid=%s): %s -- %s", namespace, sid, code, message)
            raise SocketConnectionRefused(message, {"code": code})
        await SESSION_POOL.aset(sid, user.model_dump())
        existing = await USER_POOL.aget(user.id)
        await USER_POOL.aset(user.id, (existing or []) + [sid])
        return True

    sio.on("connect", _connect, namespace=namespace)
