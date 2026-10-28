import asyncio
from contextlib import asynccontextmanager
from html import escape
from http.cookies import CookieError, SimpleCookie
import hmac
import logging
from pathlib import Path
import time
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError

from .auth import Auth, COOKIE, SESSION_SECONDS, VIEWER_COOKIE
from .models import Credentials, Curve, Override, PasswordChange, Point, Settings
from .monitor import Monitor
from .store import Store
from .socket_auth import bind_socket_sessions

STYLE = Path(__file__).with_name("style.css").read_text()


class SecurityMiddleware:
    """Also gates Socket.IO: HTTP-only auth middleware does not cover UI callbacks."""

    def __init__(self, app, auth: Auth, settings: Settings):
        self.app, self.auth, self.settings = app, auth, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        origin = headers.get(b"origin", b"").decode()
        socket = scope["path"].startswith("/_nicegui_ws")
        mutation = scope.get("method", "GET") not in ("GET", "HEAD", "OPTIONS")
        denied = (socket or mutation) and origin != self.settings.public_origin
        if socket:
            cookie = SimpleCookie()
            try:
                cookie.load(headers.get(b"cookie", b"").decode())
                token = cookie[COOKIE].value if COOKIE in cookie else ""
                viewer_token = cookie[VIEWER_COOKIE].value if VIEWER_COOKIE in cookie else ""
            except CookieError:
                token = viewer_token = ""
            denied = denied or not (await asyncio.to_thread(self.auth.session, token)
                                    or await asyncio.to_thread(self.auth.viewer_session, viewer_token))
        if denied:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return await JSONResponse({"detail": "Forbidden origin or session"}, status_code=403)(scope, receive, send)

        async def secured_send(message):
            if message["type"] == "http.response.start":
                extra = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                         (b"referrer-policy", b"same-origin")]
                if scope["path"] in ("/", "/view", "/login", "/setup", "/password", "/legal") or scope["path"].startswith("/api/"):
                    extra.append((b"cache-control", b"no-store"))
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, secured_send)


async def form_values(request: Request):
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Expected a form")
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(413, "Form too large")
    try:
        parsed = parse_qs(body.decode(), max_num_fields=6, strict_parsing=True)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "Invalid form") from exc
    if any(len(values) != 1 for values in parsed.values()):
        raise HTTPException(400, "Duplicate form fields")
    return {key: values[0] for key, values in parsed.items()}


def form_page(title, description, fields, action, *, csrf="", error="", status=200):
    controls = "".join(
        f'<label for="{name}">{label}</label><input id="{name}" name="{name}" type="{kind}" '
        f'autocomplete="{autocomplete}" required maxlength="1024">'
        for name, label, kind, autocomplete in fields)
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} · Tempmonitor</title>
    <style>{STYLE}</style></head><body><a class="admin-login" href="/login">Log in as admin</a><main class="auth-shell"><section class="auth-card">
    <div class="eyebrow">TEMPMONITOR / LOCAL CONTROL</div><h1>{escape(title)}</h1><p>{escape(description)}</p>
    <p role="alert" class="auth-error">{escape(error)}</p><form method="post" action="{action}">
    <input type="hidden" name="csrf" value="{escape(csrf)}">{controls}
    <button type="submit">{escape(title)}</button></form><p><a href="/">Back to dashboard</a> · <a href="/legal">Privacy &amp; legal</a></p></section></main></body></html>'''
    return HTMLResponse(html, status_code=status)


def create_app(settings: Settings | None = None, *, with_dashboard=True):
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir / "tempmonitor.db")
    auth = Auth(store)
    monitor = Monitor(settings, store)

    @asynccontextmanager
    async def lifespan(_):
        await monitor.start()
        try:
            yield
        finally:
            await monitor.stop()

    app = FastAPI(title="Tempmonitor", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.store, app.state.auth, app.state.monitor = settings, store, auth, monitor
    app.add_middleware(SecurityMiddleware, auth=auth, settings=settings)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        # Pydantic's default error body includes raw password/key input.
        return JSONResponse({"detail": "Invalid request fields or unsafe control values"}, status_code=422)

    async def session(request: Request):
        current = await asyncio.to_thread(auth.session, request.cookies.get(COOKIE, ""))
        if not current:
            raise HTTPException(401, "Sign in required")
        return current

    async def viewing(request: Request):
        if (await asyncio.to_thread(auth.session, request.cookies.get(COOKIE, ""))
                or await asyncio.to_thread(auth.viewer_session, request.cookies.get(VIEWER_COOKIE, ""))):
            return
        raise HTTPException(401, "Viewing password or administrator sign in required")

    async def control(request: Request, current=Depends(session)):
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), current["csrf"]):
            raise HTTPException(403, "CSRF token required")
        return current

    login_fields = [("username", "Username", "text", "username"),
                    ("password", "Password", "password", "current-password")]
    setup_fields = [("setup_token", "Setup token", "password", "off"),
                    ("username", "Administrator name", "text", "username"),
                    ("password", "Password (12 characters minimum)", "password", "new-password")]
    password_fields = [("current_password", "Current password", "password", "current-password"),
                       ("new_password", "New password (12 characters minimum)", "password", "new-password")]

    viewer_fields = [("password", "Viewing password", "password", "current-password")]

    @app.get("/view")
    async def viewer_get():
        return form_page("View dashboard", "Enter the shared password for read-only access.", viewer_fields, "/view")

    @app.post("/view")
    async def viewer_post(request: Request):
        data = await form_values(request)
        try:
            token = await asyncio.to_thread(auth.viewer_login, data.get("password", ""),
                                            request.client.host if request.client else "local")
        except (PermissionError, TimeoutError) as exc:
            return form_page("View dashboard", "Enter the shared password for read-only access.",
                             viewer_fields, "/view", error=str(exc),
                             status=429 if isinstance(exc, TimeoutError) else 401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(VIEWER_COOKIE, token, max_age=SESSION_SECONDS, secure=settings.secure_cookies,
                            httponly=True, samesite="strict", path="/")
        return response

    @app.get("/legal", response_class=HTMLResponse)
    async def legal():
        return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1"><title>Privacy &amp; legal · Tempmonitor</title>
        <style>{STYLE}</style></head><body><main class="auth-shell"><article class="auth-card legal-card">
        <div class="eyebrow">TEMPMONITOR</div><h1>Privacy &amp; legal</h1>
        <h2>Operator / Offenlegung (§ 25 MedienG)</h2>
        <p>Emil Pobinger · Perchtoldsdorf, Austria<br><a href="mailto:org@pobinger.at">org@pobinger.at</a></p>
        <p>Private, noncommercial monitoring of homeserver temperatures and fans.</p>
        <h2>Privacy &amp; essential cookies</h2>
        <p>The operator is responsible for this service. Connection data (including IP addresses), login attempts,
        and session identifiers are processed to deliver and secure access, based on the legitimate interest
        in operating and protecting this private monitor (Article 6(1)(f) GDPR).</p>
        <p>The application sets only essential first-party login cookies: <strong>tm_viewer</strong> for viewing and
        <strong>tm_session</strong> for administrator access. They expire after seven days, are HttpOnly and
        SameSite=Strict, and use Secure on HTTPS. The application uses no analytics, advertising or tracking cookies.
        These necessary cookies do not require consent. You can remove them in your browser; access will require signing in again.</p>
        <p>Session tokens are stored as hashes in local SQLite. Administrator account details and the password hash
        remain until the account is removed. Login-attempt records are used for a five-minute rate limit and
        removed on a subsequent login attempt; expired sessions are removed on subsequent logins.
        The application disables HTTP access logs. Hardware history is kept for {settings.retention_days} days.</p>
        <p>This deployment uses Cloudflare Tunnel to deliver HTTPS traffic. Cloudflare processes connection data
        as an infrastructure provider and may process data outside the EEA under its applicable safeguards.
        Its handling and retention of connection data are described in the
        <a href="https://www.cloudflare.com/privacypolicy/">Cloudflare privacy policy</a>.</p>
        <p>No visitor account is required for viewing. Providing connection data and allowing the relevant login
        cookie are necessary for access. There is no automated decision-making or profiling.</p>
        <p>Contact org@pobinger.at to request access, correction, erasure, restriction or portability where applicable,
        or to object to processing based on legitimate interests. You may complain to the
        <a href="https://www.dsb.gv.at/">Austrian Data Protection Authority</a>.</p>
        <p><a href="/">Back to dashboard</a> · <a href="/login">Log in as admin</a></p>
        </article></main></body></html>'''

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/setup")
    async def setup_get():
        if await asyncio.to_thread(auth.has_admin):
            return RedirectResponse("/login", status_code=303)
        return form_page("Create administrator", "Use the setup token stored locally in data/setup-token.",
                         setup_fields, "/setup")

    @app.post("/setup")
    async def setup_post(request: Request):
        data = await form_values(request)
        try:
            credentials = Credentials(username=data.get("username", ""), password=data.get("password", ""))
            await asyncio.to_thread(auth.setup, credentials, data.get("setup_token", ""))
        except (ValueError, PermissionError, OSError):
            return form_page("Create administrator", "Use your local setup token and a password of at least 12 characters.",
                             setup_fields, "/setup", error="Setup failed. Check the token and fields.", status=400)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login")
    async def login_get():
        if not await asyncio.to_thread(auth.has_admin):
            return RedirectResponse("/setup", status_code=303)
        return form_page("Sign in", "Your local administrator account.", login_fields, "/login")

    @app.post("/login")
    async def login_post(request: Request):
        data = await form_values(request)
        try:
            credentials = Credentials(username=data.get("username", ""), password=data.get("password", ""))
            token = await asyncio.to_thread(auth.login, credentials, request.client.host if request.client else "local")
        except TimeoutError:
            return form_page("Sign in", "Your local administrator account.", login_fields, "/login",
                             error="Too many attempts. Try again in five minutes.", status=429)
        except (ValueError, PermissionError):
            return form_page("Sign in", "Your local administrator account.", login_fields, "/login",
                             error="Invalid username or password.", status=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=settings.secure_cookies,
                            httponly=True, samesite="strict", path="/")
        return response

    @app.post("/logout")
    async def logout(request: Request, current=Depends(session)):
        data = await form_values(request)
        if not hmac.compare_digest(data.get("csrf", ""), current["csrf"]):
            raise HTTPException(403, "CSRF token required")
        await asyncio.to_thread(auth.logout, request.cookies.get(COOKIE, ""))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, secure=settings.secure_cookies, httponly=True, samesite="strict")
        return response

    @app.get("/password")
    async def password_get(request: Request):
        current = await asyncio.to_thread(auth.session, request.cookies.get(COOKIE, ""))
        if not current:
            return RedirectResponse("/login", status_code=303)
        return form_page("Change password", "Changing your password signs out all sessions.",
                         password_fields, "/password", csrf=current["csrf"])

    @app.post("/password")
    async def password_post(request: Request, current=Depends(session)):
        data = await form_values(request)
        if not hmac.compare_digest(data.get("csrf", ""), current["csrf"]):
            raise HTTPException(403, "CSRF token required")
        try:
            change = PasswordChange(current_password=data.get("current_password", ""),
                                    new_password=data.get("new_password", ""))
            await asyncio.to_thread(auth.change_password, request.cookies.get(COOKIE, ""), change)
        except (ValueError, PermissionError):
            return form_page("Change password", "Changing your password signs out all sessions.",
                             password_fields, "/password", csrf=current["csrf"],
                             error="Check your current password and use at least 12 characters for the new one.", status=400)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, secure=settings.secure_cookies, httponly=True, samesite="strict")
        return response

    @app.get("/api/session")
    async def get_session(current=Depends(session)):
        return {"username": current["username"], "csrf": current["csrf"]}

    @app.get("/api/status")
    async def get_status(_current=Depends(viewing)):
        return monitor.snapshot()

    @app.get("/api/history")
    async def get_history(hours: int = 1, _current=Depends(viewing)):
        if hours not in (1, 6, 24):
            raise HTTPException(422, "History range must be 1, 6 or 24 hours")
        return await asyncio.to_thread(store.history, hours)

    @app.put("/api/channels/{channel}/curve")
    async def set_curve(channel: int, curve: Curve, _current=Depends(control)):
        try:
            await monitor.set_curve(channel, curve)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "Controller unavailable; update was not confirmed") from exc
        return {"confirmed": True}

    @app.put("/api/channels/{channel}/override")
    async def set_override(channel: int, override: Override, _current=Depends(control)):
        try:
            await monitor.override(channel, override)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "Controller unavailable; update was not confirmed") from exc
        return {"confirmed": True}

    if with_dashboard:
        build_dashboard(app, auth, monitor, store, settings)
    return app


def build_dashboard(app, auth, monitor, store, settings):
    from nicegui import ui

    def chart_options(names, *, unit="°C", colors=None):
        return {"animation": False, "color": colors or ["#31595c", "#ae7644", "#8b9b7a", "#a89791"],
                "tooltip": {"trigger": "axis"}, "legend": {"bottom": 0, "textStyle": {"color": "#78716c"}},
                "grid": {"top": 15, "right": 18, "bottom": 50, "left": 45},
                "xAxis": {"type": "time", "axisLabel": {"color": "#78716c"}},
                "yAxis": {"type": "value", "name": unit, "splitLine": {"lineStyle": {"color": "#f0eeec"}}},
                "series": [{"name": n, "type": "line", "showSymbol": False, "smooth": False,
                            "connectNulls": False, "data": []} for n in names]}

    @ui.page("/", title="Tempmonitor")
    async def dashboard(request: Request):
        token = request.cookies.get(COOKIE, "")
        current = await asyncio.to_thread(auth.session, token)
        viewer_token = request.cookies.get(VIEWER_COOKIE, "")
        if not current and not await asyncio.to_thread(auth.viewer_session, viewer_token):
            return RedirectResponse("/view" if auth.has_admin() else "/setup", status_code=303)
        ui.colors(primary="#31595c", secondary="#ae7644", accent="#31595c")
        ui.add_css(STYLE)
        client = ui.context.client

        async def actor(*, admin=False):
            if not (await asyncio.to_thread(auth.session, token)
                    or (not admin and await asyncio.to_thread(auth.viewer_session, viewer_token))):
                ui.navigate.to("/login" if admin else "/view")
                raise PermissionError("Session expired")

        async def protect(action):
            with client:
                try:
                    await actor(admin=True)
                    await action()
                except PermissionError:
                    pass
                except (ValueError, ValidationError):
                    ui.notify("Check the values: use increasing temperatures and reach 100% by 80°C.", type="negative")
                except Exception:
                    ui.notify("The controller did not confirm the update. Check device health.", type="negative")

        with ui.header().classes("bg-white text-stone-800 items-center px-7 py-3 gap-3"):
            ui.icon("thermostat", size="26px", color="primary")
            with ui.column().classes("gap-0"):
                ui.label("Tempmonitor").classes("brand-title")
                ui.label("HOMESERVER / THERMAL CONTROL").classes("eyebrow brand-subtitle")
            ui.space()
            if current:
                ui.link("Password", "/password").classes("subtle text-sm")
                ui.html(f'<form method="post" action="/logout"><input type="hidden" name="csrf" '
                        f'value="{escape(current["csrf"])}"><button class="logout" type="submit">Sign out</button></form>',
                        sanitize=False)
            else:
                ui.link("Log in as admin", "/login").classes("subtle text-sm")

        metrics = {}
        channel_widgets = {}

        def metric(key, title, icon):
            with ui.column().classes("panel gap-3"):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(title).classes("eyebrow")
                    ui.icon(icon, color="primary", size="20px")
                metrics[key] = ui.label("—").classes("metric")

        with ui.column().classes("dashboard"):
            with ui.row().classes("items-end justify-between w-full gap-3"):
                with ui.column().classes("gap-2"):
                    ui.label("THERMAL OVERVIEW").classes("eyebrow")
                    ui.label("Homeserver temperatures").classes("page-title")
                    ui.label("GPU and environment monitoring. Fan control stays on the device.").classes("subtle text-sm")
                mode = ui.label("Connecting…").classes("status-pill")
            if settings.mock:
                ui.label("DEVELOPMENT MODE · Simulated hardware. No commands are sent to a physical controller.").classes("banner")
            alert = ui.label("").classes("banner")
            alert.set_visibility(False)
            with ui.element("section").classes("metrics-grid").props('aria-label="Temperature overview"'):
                metric("gpu", settings.gpu_label, "memory")
                metric("environment", settings.upper_label, "device_thermostat")
                metric("external", settings.lower_label, "device_thermostat")
                metric("humidity", settings.upper_label + " · humidity", "water_drop")
                metric("external_humidity", settings.lower_label + " · humidity", "water_drop")
                metric("control", "Control temperature", "shield")
            with ui.element("section").classes("charts-grid").props('aria-label="Live charts"'):
                with ui.column().classes("panel gap-1"):
                    with ui.row().classes("items-center justify-between w-full"):
                        ui.label("Temperature history").classes("panel-title")
                        history_range = ui.select({1: "Last hour", 6: "Last 6 hours", 24: "Last 24 hours"},
                                                  value=1).props("dense outlined").style("width:145px")
                    temperature_chart = ui.echart(chart_options([settings.gpu_label, settings.upper_label, settings.lower_label])).classes("large-chart")
                with ui.column().classes("panel gap-1"):
                    ui.label("Fan speed").classes("panel-title")
                    ui.label("One tachometer reading per channel, including fan pairs").classes("subtle text-xs")
                    rpm_chart = ui.echart(chart_options([f"Fan {i}" for i in range(1, 5)], unit="RPM")).classes("large-chart")
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Fan channels").classes("section-heading")
                ui.label("Curves run autonomously on the ESP32").classes("subtle text-xs")

            async def edit_curve(channel):
                await actor(admin=True)
                curve = monitor.curves[channel - 1]
                with ui.dialog() as dialog, ui.card().classes("p-6 gap-4 w-full").style("max-width:500px"):
                    ui.label(f"Fan {channel} curve").classes("section-heading")
                    ui.label("Four points, increasing temperatures. Reach 100% by 80°C.").classes("subtle text-sm")
                    minimum = ui.number("Minimum running PWM", value=curve.minimum_duty, min=5, max=100,
                                        step=1, suffix="%").props("outlined dense").classes("w-full")
                    inputs = []
                    for point in curve.points:
                        with ui.row().classes("w-full flex-nowrap gap-4"):
                            temp = ui.number("Temperature", value=point.temperature, min=0, max=80, step=1,
                                             suffix="°C").props("outlined dense").classes("w-1/2")
                            duty = ui.number("PWM duty", value=point.duty, min=0, max=100, step=1,
                                             suffix="%").props("outlined dense").classes("w-1/2")
                            inputs.append((temp, duty))

                    async def save():
                        updated = Curve(minimum_duty=int(minimum.value),
                                        points=[Point(temperature=t.value, duty=int(d.value)) for t, d in inputs])
                        await monitor.set_curve(channel, updated)
                        dialog.close()
                        ui.notify(f"Fan {channel} curve confirmed", type="positive")
                        await refresh()

                    with ui.row().classes("w-full justify-end"):
                        ui.button("Cancel", on_click=dialog.close).props("flat")
                        ui.button("Save curve", on_click=lambda: protect(save))
                dialog.open()

            async def apply_override(channel):
                widgets = channel_widgets[channel]
                override = Override(duty=int(widgets["slider"].value), seconds=widgets["duration"].value)
                await monitor.override(channel, override)
                ui.notify(f"Fan {channel} override confirmed", type="positive")
                await refresh()

            async def automatic(channel):
                await monitor.override(channel, Override(duty=0, seconds=0))
                ui.notify(f"Fan {channel} returned to automatic", type="positive")
                await refresh()

            with ui.element("section").classes("fan-grid").props('aria-label="Fan controls"'):
                for channel in range(1, 5):
                    with ui.column().classes("panel fan-card"):
                        with ui.row().classes("items-center justify-between w-full"):
                            ui.label(f"FAN {channel:02}").classes("eyebrow")
                            override_label = ui.label("Automatic").classes("subtle text-xs")
                        ui.label(settings.channel_labels[channel - 1]).classes("subtle text-xs")
                        minimum_label = ui.label("").classes("subtle text-xs")
                        with ui.row().classes("items-baseline w-full gap-2"):
                            reading = ui.label("— RPM").classes("fan-reading")
                            duty_label = ui.label("— PWM").classes("subtle text-sm")
                        preview = ui.echart({"animation": False, "grid": {"top": 10, "bottom": 25, "left": 25, "right": 10},
                            "xAxis": {"type": "value", "min": 25, "max": 80, "axisLabel": {"fontSize": 10}},
                            "yAxis": {"type": "value", "min": 0, "max": 100, "axisLabel": {"fontSize": 10}},
                            "series": [{"type": "line", "data": [], "color": "#31595c", "symbolSize": 5,
                                        "areaStyle": {"color": "#31595c", "opacity": .06}}]}).classes("small-chart")
                        buttons = ()
                        if current:
                            edit = ui.button("Edit curve", icon="tune", on_click=lambda c=channel: protect(lambda: edit_curve(c))).props("outline").classes("w-full")
                            ui.separator()
                            ui.label("Temporary override").classes("text-sm font-medium")
                            slider = ui.slider(min=0, max=100, value=50, step=1).props(f'label aria-label="Fan {channel} override duty"').classes("w-full")
                            duration = ui.select({60: "1 minute", 300: "5 minutes", 900: "15 minutes"}, value=300,
                                                 label="Duration").props("outlined dense").classes("w-full")
                            with ui.row().classes("w-full flex-nowrap gap-2"):
                                apply = ui.button("Apply", on_click=lambda c=channel: protect(lambda: apply_override(c))).classes("flex-1")
                                auto = ui.button("Auto", on_click=lambda c=channel: protect(lambda: automatic(c))).props("outline").classes("flex-1")
                            ui.label("Thermal demand and failsafe always take priority.").classes("subtle text-xs")
                            buttons = (edit, apply, auto)
                        channel_widgets[channel] = {"rpm": reading, "duty": duty_label, "override": override_label,
                            "preview": preview, "minimum": minimum_label, "buttons": buttons}
                        if current:
                            channel_widgets[channel].update(slider=slider, duration=duration)

            with ui.element("section").classes("health-grid").props('aria-label="Device and network health"'):
                with ui.column().classes("panel gap-3"):
                    ui.label("Device health").classes("panel-title")
                    device_label = ui.label("Awaiting controller").classes("text-sm")
                    gpu_health = ui.label("").classes("subtle text-sm")
                    sensors_table = ui.table(columns=[{"name": "name", "label": "Sensor", "field": "name", "align": "left"},
                        {"name": "value", "label": "Reading", "field": "reading"},
                        {"name": "health", "label": "Health", "field": "health"}], rows=[], row_key="name").props("flat dense").classes("w-full")
                with ui.column().classes("panel gap-3"):
                    ui.label("Isolated hotspot").classes("panel-title")
                    hotspot_label = ui.label("homeserver-2g").classes("font-medium")
                    hotspot_health = ui.label("").classes("subtle text-sm")
                    clients_table = ui.table(columns=[{"name": "hostname", "label": "Connected device", "field": "hostname", "align": "left"},
                        {"name": "ip", "label": "DHCP address", "field": "ip"}], rows=[], row_key="mac").props("flat dense").classes("w-full")
            updated_label = ui.label("").classes("subtle text-xs")
            ui.link("Privacy & legal", "/legal").classes("subtle text-xs")

        def value(sensors, key):
            return sensors.get(key, {}).get("value")

        def display(number, suffix):
            return "—" if number is None else f"{number:,.0f}{suffix}"

        async def refresh():
            try:
                await actor()
            except PermissionError:
                return
            snap = monitor.snapshot()
            device, hotspot = snap["device"], snap["hotspot"]
            sensors = device["sensors"]
            temps = [g["temperature"] for g in snap["gpus"] if g["temperature"] is not None]
            metrics["gpu"].set_text(display(max(temps) if temps else None, "°C"))
            metrics["environment"].set_text(display(value(sensors, "environment_temperature"), "°C"))
            metrics["humidity"].set_text(display(value(sensors, "environment_humidity"), "%"))
            metrics["external"].set_text(display(value(sensors, "external_temperature"), "°C"))
            metrics["external_humidity"].set_text(display(value(sensors, "external_humidity"), "%"))
            metrics["control"].set_text(display(value(sensors, "control_temperature"), "°C"))
            mode.set_text(value(sensors, "control_mode") or "Controller offline")
            problem = snap["error"] or ("ESP32 failsafe is active: fans are commanded to 100%." if value(sensors, "failsafe") else "")
            alert.set_text(problem)
            alert.set_visibility(bool(problem))
            for channel, widgets in channel_widgets.items():
                widgets["rpm"].set_text(display(value(sensors, f"fan_{channel}_speed"), " RPM"))
                widgets["duty"].set_text(display(value(sensors, f"fan_{channel}_duty"), "% PWM"))
                remaining = value(sensors, f"fan_{channel}_override_remaining")
                widgets["override"].set_text(f"Override · {remaining:.0f}s" if remaining else "Automatic")
                widgets["minimum"].set_text(f"Running minimum {monitor.curves[channel - 1].minimum_duty}% · 2s startup boost")
                widgets["preview"].options["series"][0]["data"] = [[p.temperature, p.duty] for p in monitor.curves[channel - 1].points]
                widgets["preview"].update()
                for button in widgets["buttons"]:
                    button.set_enabled(device["connected"])
            info = device["info"]
            connection = "Simulated" if settings.mock else "Encrypted native API" if device["connected"] else "Disconnected"
            device_label.set_text(f"{info.get('name', settings.device_host)} · {connection} · {info.get('firmware', 'unknown firmware')}")
            gpu_health.set_text(" · ".join(f"{g['name']}: {g['error'] or ('stale' if g['stale'] else display(g['temperature'], '°C'))}" for g in snap["gpus"]))
            sensors_table.rows = [{"name": s["name"], "reading": f"{s['value']} {s['unit']}" if s["value"] is not None else "—",
                                  "health": "Stale / unavailable" if s["stale"] or s["value"] is None else "Live"}
                                 for s in sensors.values() if s["unit"] in ("°C", "%") and not s["name"].startswith("Fan")]
            sensors_table.update()
            hotspot_label.set_text(f"{hotspot.get('ssid', 'homeserver-2g')} · {hotspot.get('subnet', 'status unavailable')}")
            healthy = not hotspot.get("stale") and hotspot.get("ap_active") and hotspot.get("isolated")
            hotspot_health.set_text(("Local only · forwarding blocked" if healthy else "Host status unavailable or needs attention") +
                                   (" · boot enabled" if hotspot.get("boot_enabled") else ""))
            clients_table.rows = hotspot.get("clients", []) if not hotspot.get("stale") else []
            clients_table.update()
            updated_label.set_text("Updated " + time.strftime("%H:%M:%S") + " · 7-second GPU sampling · local SQLite history")

        async def refresh_charts():
            try:
                await actor()
            except PermissionError:
                return
            history = await asyncio.to_thread(store.history, history_range.value)
            series = [[], [], []]
            rpm_series = [[], [], [], []]
            for sample in history:
                timestamp = sample["at"] * 1000
                temps = [g["temperature"] for g in sample["gpus"] if g["temperature"] is not None]
                sensors = sample["device"]["sensors"]
                for target, reading in zip(series, [max(temps) if temps else None,
                                                   value(sensors, "environment_temperature"), value(sensors, "external_temperature")], strict=True):
                    target.append([timestamp, reading])
                for channel, target in enumerate(rpm_series, 1):
                    target.append([timestamp, value(sensors, f"fan_{channel}_speed")])
            for widget, data in ((temperature_chart, series), (rpm_chart, rpm_series)):
                for target, values in zip(widget.options["series"], data, strict=True):
                    target["data"] = values
                widget.update()

        history_range.on_value_change(refresh_charts)
        await refresh()
        await refresh_charts()
        ui.timer(2, refresh)
        ui.timer(7, refresh_charts)

    ui.run_with(app, title="Tempmonitor", favicon="🌡️", dark=False, show_welcome_message=False)
    bind_socket_sessions(auth)


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, proxy_headers=False,
                access_log=False, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
