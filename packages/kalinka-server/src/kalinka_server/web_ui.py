"""Serve the browser player bundle (the optional ``kalinka-web`` package).

The bundle lives at :func:`paths.web_ui_dir` and is mounted at ``/``. When the
package is not installed the directory is absent, so instead of a bare 404 we
render a short, self-contained page telling the admin how to install it.
"""

import os

from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

# Self-contained: the bundle that would provide external assets is exactly what
# is missing here.
_NOT_INSTALLED_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalinka — web player not installed</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; padding: 24px;
    background: #0e0e10; color: #e7e7ea;
    font: 15px/1.6 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  }
  .card {
    width: 100%; max-width: 520px; padding: 40px 36px;
    background: #17171a; border: 1px solid #262629; border-radius: 16px;
    box-shadow: 0 20px 60px rgba(0,0,0,.5);
  }
  .brand {
    font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
    font-size: 12px; color: #c2394b; margin: 0 0 20px;
  }
  h1 { margin: 0 0 12px; font-size: 22px; font-weight: 600; }
  p { margin: 0 0 16px; color: #a9a9b2; }
  pre {
    margin: 0 0 8px; padding: 14px 16px;
    white-space: pre-wrap; overflow-wrap: anywhere;
    background: #0e0e10; border: 1px solid #262629; border-radius: 10px;
    font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; color: #e7e7ea;
  }
  code { color: #d8556a; }
  .hint { font-size: 13px; color: #6f6f78; margin-bottom: 0; }
  a { color: #d8556a; }
</style>
</head>
<body>
  <div class="card">
    <p class="brand">Kalinka</p>
    <h1>Web player not installed</h1>
    <p>
      The server is running, but the browser player package
      (<code>kalinka-web</code>) is not installed on this machine.
    </p>
    <p>Install it, then reload this page:</p>
    <pre>curl -fsSL https://kalinkaplayer.com/install.sh | sudo bash</pre>
    <p class="hint">
      The installer adds the web player alongside the server. It appears here
      immediately — no restart needed.
    </p>
  </div>
</body>
</html>
"""


class WebUiStaticFiles(StaticFiles):
    """``StaticFiles`` that renders an install page when the bundle is absent.

    A genuine 404 for a path *within* an installed bundle is passed through
    unchanged; only requests that miss because nothing is installed get the
    friendly page.
    """

    async def check_config(self) -> None:
        # An absent bundle is expected (package not installed); skip Starlette's
        # missing-directory check, which otherwise 500s on the first request
        # even with check_dir=False. get_response renders the install page.
        return

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404 and not self._bundle_installed():
                if scope["method"] == "HEAD":
                    return Response(status_code=200)
                return HTMLResponse(_NOT_INSTALLED_PAGE, status_code=200)
            raise

    def _bundle_installed(self) -> bool:
        return os.path.isfile(os.path.join(self.directory, "index.html"))


class CrossOriginIsolationMiddleware:
    """ASGI middleware setting COOP/COEP so a browser cross-origin-isolates
    the page, letting the web UI's WASM build run skwasm multi-threaded.
    Without this it still works — skwasm falls back to single-threaded.

    ``credentialless`` rather than ``require-corp``: plugins (Jamendo, etc.)
    hand back album-art URLs straight from third-party CDNs, fetched without
    credentials. ``require-corp`` would need those CDNs to send a CORP
    header we have no control over; ``credentialless`` only enforces one for
    credentialed requests.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_coi_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Embedder-Policy"] = "credentialless"
            await send(message)

        await self._app(scope, receive, send_with_coi_headers)
