"""Exercise the deployed ASGI server over real TCP and Redis, without Daphne.

The child process uses Django's disposable test database. Its server arguments
come from Compose so transport regressions cannot hide behind an ASGI test double.
"""

import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from channels_redis.core import RedisChannelLayer
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
import pytest
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.sync.client import connect

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.channels.models import WhatsAppAccount
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.django_db(transaction=True)


def _compose_command(filename):
    text = (ROOT / filename).read_text(encoding="utf-8")
    ws = text.split("\n  ws:\n", 1)[1].split("\n  worker:\n", 1)[0]
    command = ws.split("\n    command:\n", 1)[1].split("\n    expose:", 1)[0]
    return [
        shlex.split(line.strip()[2:])[0]
        for line in command.splitlines()
        if line.strip().startswith("- ")
    ]


class ASGIProcess:
    def __init__(self, log_path):
        self.command = _compose_command("docker-compose.yml")
        assert self.command == _compose_command("docker-compose.staging.yml")
        assert self.command[:2] == ["uvicorn", "config.asgi:application"]
        self.log_path = log_path
        self.process = None
        self.log = None

    def start(self):
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            self.port = candidate.getsockname()[1]
        command = self.command.copy()
        command[command.index("--host") + 1] = "127.0.0.1"
        command[command.index("--port") + 1] = str(self.port)
        self.origin = f"http://127.0.0.1:{self.port}"
        self.ws_origin = f"ws://127.0.0.1:{self.port}"
        env = os.environ.copy()
        env.update({
            "DJANGO_SETTINGS_MODULE": "config.settings.testing",
            "SECRET_KEY": settings.SECRET_KEY,
            "ALLOWED_HOSTS": "127.0.0.1,localhost",
            "CHANNEL_LAYER_REDIS_URL": settings.CHANNEL_LAYER_REDIS_URL,
            "REDIS_URL": settings.REDIS_URL,
            "CACHE_REDIS_URL": settings.CACHES["default"]["LOCATION"],
            "PYTHONUNBUFFERED": "1",
        })
        for key in ("NAME", "USER", "PASSWORD", "HOST", "PORT"):
            env[f"DB_{key}"] = str(connection.settings_dict[key] or "")
        self.log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", *command],
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail(f"ASGI startup failed: {self.logs()}")
            try:
                if self.get_json("/health/live/")["status"] == "ok":
                    return
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(0.05)
        pytest.fail(f"ASGI startup timed out: {self.logs()}")

    def get_json(self, path):
        # Ignore developer-machine proxies for this loopback-only fixture.
        with build_opener(ProxyHandler({})).open(self.origin + path, timeout=1) as response:
            assert response.status == 200
            return json.load(response)

    def websocket(self, path="/ws/whatsapp/inbox/", *, cookie=None, origin=None):
        return connect(
            self.ws_origin + path,
            origin=origin or self.origin,
            additional_headers={"Cookie": cookie} if cookie else None,
            proxy=None, open_timeout=5, close_timeout=3,
        )

    def logs(self):
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]

    def stop(self):
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
                    pytest.fail(f"ASGI did not shut down gracefully: {self.logs()}")
        finally:
            if self.log:
                self.log.close()


@pytest.fixture
def asgi_server(transactional_db, tmp_path):
    server = ASGIProcess(tmp_path / "asgi.log")
    try:
        server.start()
        yield server
    finally:
        server.stop()


@pytest.fixture
def tenants(transactional_db):
    result = []
    for index in range(2):
        organization = Organization.objects.create(name=f"ASGI tenant {index}", package="dfy")
        user = User.objects.create_user(
            email=f"asgi-{index}@example.com", name="ASGI admin",
            organization=organization, role=User.Role.ADMIN,
        )
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        pipeline = Pipeline.objects.create(organization=organization, name="Sales")
        stage = Stage.objects.create(pipeline=pipeline, name="New")
        lead = Lead.objects.create(
            organization=organization, pipeline=pipeline, stage=stage,
            name="ASGI fixture", phone=f"+91900000000{index}", ai_enabled=False,
        )
        account = WhatsAppAccount.objects.create(
            organization=organization, connection_type="hosted", is_active=True,
        )
        result.append({
            "organization": organization, "lead": lead, "account": account,
            "cookie": f"{get_session_cookie_name('dashboard')}={session.session_key}",
        })
    return result


def _send_event(group, event):
    async def send():
        layer = RedisChannelLayer(**settings.CHANNEL_LAYERS["default"]["CONFIG"])
        try:
            await layer.group_send(group, event)
        finally:
            await layer.close_pools()

    asyncio.run(send())


def _inbox_event(lead, preview):
    return {
        "type": "whatsapp.inbox_update", "lead_id": str(lead.pk),
        "unread_count": 1, "last_message_at": "2026-10-16T00:00:00Z",
        "last_message_preview": preview,
    }


def test_http_and_authenticated_redis_delivery_survive_close_and_reconnect(asgi_server, tenants):
    own, foreign = tenants
    assert asgi_server.get_json("/health/ready/")["checks"] == {
        "database": True, "redis": True,
    }
    thread = f"/ws/whatsapp/{own['lead'].pk}/"
    with (
        asgi_server.websocket(thread, cookie=own["cookie"]) as first,
        asgi_server.websocket(cookie=foreign["cookie"]) as other,
    ):
        # Distinct real subscribers prove an organization broadcast stays scoped.
        for tenant, preview in ((foreign, "foreign"), (own, "own")):
            _send_event(
                f"whatsapp_inbox_{tenant['organization'].pk}",
                _inbox_event(tenant["lead"], preview),
            )
        assert json.loads(first.recv(timeout=3))["last_message_preview"] == "own"
        assert json.loads(other.recv(timeout=3))["last_message_preview"] == "foreign"
        _send_event(f"whatsapp_thread_{own['lead'].pk}", {
            "type": "whatsapp.message", "message": {"id": "local-event", "body": "fixture"},
        })
        assert json.loads(first.recv(timeout=3)) == {
            "kind": "message", "message": {"id": "local-event", "body": "fixture"},
        }
        with pytest.raises(TimeoutError):
            other.recv(timeout=0.15)
        # Actual protocol ping/pong, independent of the application's heartbeat.
        assert first.ping().wait(timeout=3)
    assert first.close_code == 1000
    with asgi_server.websocket(thread, cookie=own["cookie"]) as reconnected:
        _send_event(f"whatsapp_inbox_{own['organization'].pk}", _inbox_event(own["lead"], "again"))
        assert json.loads(reconnected.recv(timeout=3))["last_message_preview"] == "again"

    hosted_path = f"/ws/whatsapp/hosted/{own['account'].pk}/"
    with asgi_server.websocket(hosted_path, cookie=own["cookie"]) as hosted:
        assert json.loads(hosted.recv(timeout=3)) == {
            "kind": "ready", "account_id": str(own["account"].pk),
        }
        hosted.send(json.dumps({"kind": "ping"}))
        assert json.loads(hosted.recv(timeout=3)) == {"kind": "pong"}
        _send_event(f"hosted_whatsapp_{own['account'].pk}", {
            "type": "hosted.message", "account_id": str(own["account"].pk),
            "organization_id": str(own["organization"].pk), "message_id": "hosted-local",
        })
        assert json.loads(hosted.recv(timeout=3))["message_id"] == "hosted-local"


def test_anonymous_wrong_cookie_origin_and_foreign_tenant_are_rejected(asgi_server, tenants):
    own, foreign = tenants
    rejected = [
        {},
        {"cookie": "shvya_crm_sessionid=invalid-session"},
        {"cookie": own["cookie"].replace("shvya_crm_sessionid=", "sessionid=")},
        {"cookie": own["cookie"], "origin": "https://untrusted.invalid"},
        {"cookie": own["cookie"], "path": f"/ws/whatsapp/{foreign['lead'].pk}/"},
        {"cookie": own["cookie"], "path": f"/ws/whatsapp/hosted/{foreign['account'].pk}/"},
    ]
    for arguments in rejected:
        with pytest.raises(InvalidStatus) as failure:
            with asgi_server.websocket(**arguments):
                pytest.fail("Unauthorized WebSocket handshake was accepted")
        assert failure.value.response.status_code == 403
    own["organization"].is_active = False
    own["organization"].save(update_fields=["is_active"])
    with pytest.raises(InvalidStatus) as failure:
        with asgi_server.websocket(cookie=own["cookie"]):
            pytest.fail("Inactive organization's WebSocket handshake was accepted")
    assert failure.value.response.status_code == 403
    assert asgi_server.get_json("/health/live/")["status"] == "ok"


def test_oversized_websocket_frame_closes_only_that_connection(asgi_server, tenants):
    own = tenants[0]
    limit = int(asgi_server.command[asgi_server.command.index("--ws-max-size") + 1])
    assert limit == 1048576
    with asgi_server.websocket(cookie=own["cookie"]) as client:
        client.send(json.dumps({"payload": "x" * limit}))
        with pytest.raises(ConnectionClosedError) as failure:
            client.recv(timeout=3)
        assert failure.value.rcvd.code == 1009
    with asgi_server.websocket(cookie=own["cookie"]) as client:
        assert client.ping().wait(timeout=3)


def test_sigterm_closes_active_socket_and_session_reconnects_after_restart(asgi_server, tenants):
    own = tenants[0]
    with asgi_server.websocket(cookie=own["cookie"]) as client:
        asgi_server.process.terminate()
        with pytest.raises(ConnectionClosedError) as failure:
            client.recv(timeout=5)
        assert failure.value.rcvd.code == 1012
        # Uvicorn re-raises the handled signal after its graceful shutdown.
        assert asgi_server.process.wait(timeout=10) in (0, -signal.SIGTERM)
        assert "Finished server process" in asgi_server.logs()
    asgi_server.stop()
    asgi_server.start()
    with asgi_server.websocket(cookie=own["cookie"]) as client:
        _send_event(f"whatsapp_inbox_{own['organization'].pk}", _inbox_event(own["lead"], "restart"))
        assert json.loads(client.recv(timeout=3))["last_message_preview"] == "restart"
