"""Bound WebSocket authorization age and cleanup without a database or server."""

import asyncio

import pytest

from apps.channels.asgi import WebSocketLifecycleMiddleware


class Connection:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.messages.put_nowait({"type": "websocket.connect"})
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    async def receive(self):
        return await self.messages.get()

    async def run(self, app, **limits):
        middleware = WebSocketLifecycleMiddleware(app, **limits)
        await middleware({"type": "websocket"}, self.receive, self.send)


def run(scenario):
    async def checked():
        await asyncio.wait_for(scenario(), timeout=2)
        # Nested receive/watchdog tasks must not survive a closed connection.
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(checked())


def test_defaults_preserve_previous_server_deadlines():
    middleware = WebSocketLifecycleMiddleware(None)
    assert (middleware.lifetime, middleware.handshake_timeout, middleware.cleanup_timeout) == (
        86400, 5, 10,
    )


def test_http_call_is_forwarded_without_wrapping_or_timeout():
    async def scenario():
        connection = Connection()
        scope = {"type": "http"}
        receive, send = connection.receive, connection.send

        async def app(actual_scope, actual_receive, actual_send):
            assert (actual_scope, actual_receive, actual_send) == (scope, receive, send)
            await asyncio.sleep(0.02)
            return "unchanged"

        middleware = WebSocketLifecycleMiddleware(app, handshake_timeout=0.001, lifetime=0.001)
        assert await middleware(scope, receive, send) == "unchanged"

    run(scenario)


def test_client_disconnect_reaches_application_without_an_extra_close():
    async def scenario():
        connection = Connection()
        disconnected = []

        async def app(scope, receive, send):
            assert (await receive())["type"] == "websocket.connect"
            await send({"type": "websocket.accept"})
            connection.messages.put_nowait({"type": "websocket.disconnect", "code": 1000})
            disconnected.append(await receive())

        await connection.run(app)
        assert connection.sent == [{"type": "websocket.accept"}]
        assert disconnected == [{"type": "websocket.disconnect", "code": 1000}]

    run(scenario)


def test_authentication_denial_is_preserved():
    async def scenario():
        connection = Connection()

        async def app(scope, receive, send):
            await receive()
            await send({"type": "websocket.close", "code": 4001})

        await connection.run(app)
        assert connection.sent == [{"type": "websocket.close", "code": 4001}]

    run(scenario)


def test_handshake_timeout_closes_and_cancels_stalled_authentication():
    async def scenario():
        connection = Connection()
        cancelled = asyncio.Event()

        async def app(scope, receive, send):
            try:
                await receive()
                await asyncio.Future()
            finally:
                cancelled.set()

        await connection.run(app, handshake_timeout=0.01, cleanup_timeout=0.02)
        assert connection.sent == [{"type": "websocket.close", "code": 1001}]
        assert cancelled.is_set()

    run(scenario)


def test_late_authentication_cannot_accept_or_broadcast_after_timeout():
    async def scenario():
        connection = Connection()
        disconnected = []

        async def app(scope, receive, send):
            await receive()
            await asyncio.sleep(0.03)
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.send", "text": "late broadcast"})
            disconnected.append(await receive())

        await connection.run(app, handshake_timeout=0.01, cleanup_timeout=0.2)
        assert connection.sent == [{"type": "websocket.close", "code": 1001}]
        assert disconnected == [{"type": "websocket.disconnect", "code": 1001}]

    run(scenario)


def test_lifetime_expiry_wakes_receive_and_allows_subscription_heartbeat_cleanup():
    async def scenario():
        connection = Connection()
        groups = set()
        disconnected = []
        heartbeat_stopped = asyncio.Event()

        async def heartbeat():
            try:
                await asyncio.Future()
            finally:
                heartbeat_stopped.set()

        async def app(scope, receive, send):
            await receive()
            groups.add("authorized subscription")
            task = asyncio.create_task(heartbeat())
            await send({"type": "websocket.accept"})
            try:
                disconnected.append(await receive())
                await send({"type": "websocket.send", "text": "expired broadcast"})
            finally:
                groups.clear()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        await connection.run(app, lifetime=0.03, cleanup_timeout=0.2)
        assert connection.sent == [
            {"type": "websocket.accept"}, {"type": "websocket.close", "code": 1001},
        ]
        assert disconnected == [{"type": "websocket.disconnect", "code": 1001}]
        assert not groups
        assert heartbeat_stopped.is_set()

    run(scenario)


def test_disconnected_application_cleanup_is_bounded():
    async def scenario():
        connection = Connection()
        cancelled = asyncio.Event()

        async def app(scope, receive, send):
            try:
                await receive()
                await send({"type": "websocket.accept"})
                connection.messages.put_nowait({"type": "websocket.disconnect", "code": 1000})
                await receive()
                await asyncio.Future()
            finally:
                cancelled.set()

        await connection.run(app, cleanup_timeout=0.02)
        assert cancelled.is_set()
        assert connection.sent == [{"type": "websocket.accept"}]

    run(scenario)


def test_in_progress_accept_finishes_before_timeout_close():
    async def scenario():
        connection = Connection()
        send_active = False

        async def transport_send(message):
            nonlocal send_active
            assert not send_active
            send_active = True
            try:
                if message["type"] == "websocket.accept":
                    await asyncio.sleep(0.03)
                connection.sent.append(message)
            finally:
                send_active = False

        async def app(scope, receive, send):
            await receive()
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.send", "text": "late broadcast"})
            assert (await receive())["type"] == "websocket.disconnect"

        middleware = WebSocketLifecycleMiddleware(app, handshake_timeout=0.01, cleanup_timeout=0.2)
        await middleware({"type": "websocket"}, connection.receive, transport_send)
        assert connection.sent == [
            {"type": "websocket.accept"}, {"type": "websocket.close", "code": 1001},
        ]

    run(scenario)


@pytest.mark.parametrize("exception", [RuntimeError("application failed"), TimeoutError("provider timed out")])
def test_unexpected_application_errors_propagate(exception):
    async def scenario():
        async def app(scope, receive, send):
            await receive()
            raise exception

        with pytest.raises(type(exception), match=str(exception)):
            await Connection().run(app)

    run(scenario)


def test_unexpected_disconnect_cleanup_error_propagates():
    async def scenario():
        async def app(scope, receive, send):
            await receive()
            await send({"type": "websocket.accept"})
            await receive()
            raise RuntimeError("cleanup failed")

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await Connection().run(app, lifetime=0.01)

    run(scenario)


def test_peer_closing_during_expiry_does_not_interrupt_application_cleanup():
    async def scenario():
        connection = Connection()
        cleaned = asyncio.Event()

        async def transport_send(message):
            if message["type"] == "websocket.close":
                raise OSError("peer already closed")
            connection.sent.append(message)

        async def app(scope, receive, send):
            await receive()
            await send({"type": "websocket.accept"})
            assert (await receive())["type"] == "websocket.disconnect"
            await asyncio.sleep(0.01)
            cleaned.set()

        middleware = WebSocketLifecycleMiddleware(app, lifetime=0.01, cleanup_timeout=0.2)
        await middleware({"type": "websocket"}, connection.receive, transport_send)
        assert cleaned.is_set()

    run(scenario)


def test_server_cancellation_cleans_pending_receive_and_timer_tasks():
    async def scenario():
        connection = Connection()
        accepted = asyncio.Event()
        cleaned = asyncio.Event()

        async def app(scope, receive, send):
            try:
                await receive()
                await send({"type": "websocket.accept"})
                accepted.set()
                await receive()
            finally:
                cleaned.set()

        task = asyncio.create_task(connection.run(app))
        await accepted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()

    run(scenario)


def test_application_delaying_cancellation_cannot_extend_cleanup_deadline(caplog):
    async def scenario():
        connection = Connection()
        release = asyncio.Event()
        cancelled = asyncio.Event()
        finished = asyncio.Event()

        async def app(scope, receive, send):
            await receive()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                await send({"type": "websocket.accept"})
            finally:
                finished.set()

        await asyncio.wait_for(
            connection.run(app, handshake_timeout=0.01, cleanup_timeout=0.01), timeout=0.5,
        )
        assert cancelled.is_set()
        assert not finished.is_set()
        assert "exceeded its cancellation deadline" in caplog.text
        release.set()
        await finished.wait()
        await asyncio.sleep(0)  # Let the task's completion callback retrieve its result.
        assert connection.sent == [{"type": "websocket.close", "code": 1001}]

    run(scenario)
