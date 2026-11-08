"""The TypeScript client calls only routes the API has (FR-CP-04; WP 3.6).

Its own tests run against a stand-in. This one keeps the two from drifting apart: every method and
path in ``sdks/typescript/src/index.ts`` must be one the API serves.
"""

import re
from pathlib import Path

from fastapi.routing import APIRoute

from aios.api import create_app
from aios.testing.runtime import LocalRuntime

CLIENT = Path(__file__).resolve().parents[3] / "sdks" / "typescript" / "src" / "index.ts"
CALL = re.compile(
    r'this\.(?:call|send)(?:<[^>]*>)?\(\s*"(GET|POST|PUT|DELETE)",\s*[`"]([^`"]+)[`"]'
)


def _shape(path: str) -> str:
    """``/v1/apps/${part(app)}/runs${query(...)}`` as ``/v1/apps/{}/runs``."""
    path = re.sub(r"\$\{query\([^}]*\}\)\}", "", path)
    path = re.sub(r"\$\{query\(\{[^`]*", "", path)
    return re.sub(r"\$\{[^}]+\}", "{}", path)


async def test_every_call_the_client_makes_is_a_route() -> None:
    calls = {(method, _shape(path)) for method, path in CALL.findall(CLIENT.read_text("utf-8"))}
    assert len(calls) > 20
    async with LocalRuntime() as runtime:
        app = create_app(runtime.runtime)
    routes = {
        (method, re.sub(r"\{[^}]+\}", "{}", route.path))
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    assert calls - routes == set()
