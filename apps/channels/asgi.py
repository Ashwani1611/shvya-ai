"""Server-enforced WebSocket deadlines independent of the ASGI runner."""

import asyncio
import logging


logger = logging.getLogger(__name__)


def _observe_late_application(task):
    """Retrieve a delayed task's error when cancellation could not stop it."""
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            logger.error("WebSocket application failed after cancellation (%s)", type(error).__name__)


class WebSocketLifecycleMiddleware:
    """Preserve Daphne's handshake, connection-age and cleanup bounds.

    A socket must reconnect at least daily, re-entering the existing CRM session
    and tenant authorization checks. Browser timers cannot enforce this boundary.
    Constructor limits permit short deterministic tests; production uses defaults.
    """

    def __init__(self, app, *, lifetime=86400, handshake_timeout=5, cleanup_timeout=10):
        if min(lifetime, handshake_timeout, cleanup_timeout) <= 0:
            raise ValueError("WebSocket lifecycle limits must be positive")
        self.app = app
        self.lifetime = lifetime
        self.handshake_timeout = handshake_timeout
        self.cleanup_timeout = cleanup_timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            return await self.app(scope, receive, send)

        deadline = asyncio.get_running_loop().time() + self.lifetime
        handshake = asyncio.Event()
        closed = asyncio.Event()
        forced_disconnect = asyncio.Event()
        send_lock = asyncio.Lock()

        async def guarded_send(message):
            async with send_lock:
                # An application finishing authentication after its deadline must
                # not accept, or send broadcasts on, the expired connection.
                if forced_disconnect.is_set() or closed.is_set():
                    return
                await send(message)
                kind = message["type"]
                if kind in {"websocket.accept", "websocket.close", "websocket.http.response.start"}:
                    handshake.set()
                if kind == "websocket.close" or (
                    kind == "websocket.http.response.body" and not message.get("more_body", False)
                ):
                    closed.set()

        async def guarded_receive():
            if forced_disconnect.is_set():
                return {"type": "websocket.disconnect", "code": 1001}
            incoming = asyncio.create_task(receive())
            expiry = asyncio.create_task(forced_disconnect.wait())
            try:
                await asyncio.wait({incoming, expiry}, return_when=asyncio.FIRST_COMPLETED)
                if forced_disconnect.is_set():
                    return {"type": "websocket.disconnect", "code": 1001}
                message = await incoming
                if message["type"] == "websocket.disconnect":
                    handshake.set()
                    closed.set()
                return message
            finally:
                for task in (incoming, expiry):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(incoming, expiry, return_exceptions=True)

        async def wait_for_deadline():
            try:
                await asyncio.wait_for(handshake.wait(), timeout=self.handshake_timeout)
            except TimeoutError:
                return "handshake"
            try:
                remaining = max(0, deadline - asyncio.get_running_loop().time())
                await asyncio.wait_for(closed.wait(), timeout=remaining)
            except TimeoutError:
                return "lifetime"
            return "disconnect"

        application = asyncio.create_task(self.app(scope, guarded_receive, guarded_send))
        timer = asyncio.create_task(wait_for_deadline())
        cleanup_deadline = None
        cancellation_requested = False
        try:
            await asyncio.wait({application, timer}, return_when=asyncio.FIRST_COMPLETED)
            if application.done():
                return await application

            reason = await timer
            cleanup_deadline = asyncio.get_running_loop().time() + self.cleanup_timeout
            try:
                async with asyncio.timeout(self.cleanup_timeout):
                    if reason != "disconnect":
                        # Wake an outstanding receive so Channels can execute its
                        # normal group/heartbeat cleanup even if transport input stalls.
                        forced_disconnect.set()
                        async with send_lock:
                            if not closed.is_set():
                                try:
                                    await send({"type": "websocket.close", "code": 1001})
                                except OSError:
                                    # ASGI permits OSError when the peer has
                                    # already gone; still let Channels clean up.
                                    pass
                                closed.set()
                    return await asyncio.shield(application)
            except TimeoutError:
                if application.done():
                    # An application TimeoutError is not a lifecycle timeout.
                    return await application
                application.cancel()
                cancellation_requested = True
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            if not application.done():
                if not cancellation_requested:
                    application.cancel()
                remaining = self.cleanup_timeout if cleanup_deadline is None else max(
                    0, cleanup_deadline - asyncio.get_running_loop().time()
                )
                await asyncio.wait({application}, timeout=remaining)
            if application.done():
                if not application.cancelled():
                    application.result()  # Keep unexpected completed-task errors visible.
            else:
                # Python cannot force-kill a coroutine that ignores cancellation.
                # Return at the deadline; its guarded send path remains closed.
                forced_disconnect.set()
                logger.warning("WebSocket application exceeded its cancellation deadline")
                application.add_done_callback(_observe_late_application)
