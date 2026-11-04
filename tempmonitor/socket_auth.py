import asyncio
import hmac
import inspect
from urllib.parse import parse_qs

from starlette.requests import HTTPConnection

from .auth import Auth, COOKIE, VIEWER_COOKIE


def bind_socket_sessions(auth: Auth):
    """Bind NiceGUI messages to the session which rendered their target page."""
    from nicegui import Client, core

    sio = core.sio

    async def owns_page(sid, message, environ=None):
        if not isinstance(message, dict) or not isinstance(message.get("client_id"), str):
            return False
        client = Client.instances.get(message["client_id"])
        environ = environ if environ is not None else sio.get_environ(sid)
        if client is None or not environ or "asgi.scope" not in environ:
            return False
        original = client.request.cookies
        sender = HTTPConnection(environ["asgi.scope"]).cookies
        admin_token = original.get(COOKIE, "")
        if await asyncio.to_thread(auth.session, admin_token):
            return hmac.compare_digest(admin_token.encode(), sender.get(COOKIE, "").encode())
        viewer_token = original.get(VIEWER_COOKIE, "")
        return (hmac.compare_digest(viewer_token.encode(), sender.get(VIEWER_COOKIE, "").encode())
                and await asyncio.to_thread(auth.viewer_session, viewer_token))

    def guard(handler):
        async def guarded(sid, message):
            if not await owns_page(sid, message):
                return False
            result = handler(sid, message)
            return await result if inspect.isawaitable(result) else result
        return guarded

    # NiceGUI's upstream handlers trust client_id rather than the sending socket.
    for event in ("handshake", "event", "javascript_response", "ack", "log"):
        sio.on(event, guard(sio.handlers["/"][event]))

    original_connect = sio.handlers["/"]["connect"]

    async def connect(sid, environ, auth_data=None):
        query = parse_qs(environ.get("QUERY_STRING", ""))
        if "client_id" in query and not await owns_page(sid, {"client_id": query["client_id"][0]}, environ):
            return False
        result = original_connect(sid, environ, auth_data)
        return await result if inspect.isawaitable(result) else result

    sio.on("connect", connect)
