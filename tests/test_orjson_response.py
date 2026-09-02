"""The JSON responses still go out through orjson (#401).

FastAPI deprecated its `ORJSONResponse` on the grounds that its own
Pydantic-based serialization "is faster and doesn't need a custom
response class". Measured on a 161 KB artist-shaped payload it is not:

    orjson         0.09 ms
    pydantic-core  0.58 ms
    stdlib json    0.87 ms

That encode happens under the GIL, and the artist endpoint's ~270 KB
response is built while audio is playing — minimising the hold is why
the app opted into orjson in the first place. Taking the deprecation's
advice would make it roughly six times longer.

Only the import was deprecated, not orjson, so the class is defined
locally. These tests pin that it is still doing the job, since
"serialization silently got slower" is not something the rest of the
suite would notice.
"""
from __future__ import annotations

import json

import pytest


def test_app_serializes_through_orjson():
    import server

    assert server.app.router.default_response_class is server._ORJSONResponse


def test_render_matches_json_semantics():
    import server

    payload = {"name": "Boards of Canada", "year": 1998, "ok": True, "none": None}
    rendered = server._ORJSONResponse(content=payload).body

    assert json.loads(rendered) == payload


def test_non_string_keys_are_handled():
    """OPT_NON_STR_KEYS — FastAPI's class set it, so dropping it would
    turn a working response into a 500 on any int-keyed dict."""
    import server

    rendered = server._ORJSONResponse(content={1: "a", 2: "b"}).body

    assert json.loads(rendered) == {"1": "a", "2": "b"}


def test_numpy_scalars_are_serializable():
    """OPT_SERIALIZE_NUMPY. The audio paths deal in numpy types, and
    plain json raises on them."""
    np = pytest.importorskip("numpy")
    import server

    rendered = server._ORJSONResponse(content={"peak": np.float32(0.5)}).body

    assert json.loads(rendered)["peak"] == pytest.approx(0.5)


def test_no_deprecated_fastapi_response_import():
    """The point of defining it locally. Re-importing FastAPI's version
    would bring the warning back without changing behaviour."""
    import pathlib

    src = pathlib.Path("server.py").read_text(encoding="utf-8")

    assert "from fastapi.responses import ORJSONResponse" not in src
