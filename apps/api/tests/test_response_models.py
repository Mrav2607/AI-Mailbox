"""Every route declares what it returns, and means it.

Two things are being guarded here. First, a route without a response_model
returns whatever dict it happens to build, so a stray field (an internal id, a
token, a raw exception) reaches the client and the OpenAPI schema says nothing.
Second, our output models forbid extras, which is what turns "I forgot a field"
from a silent 200 with a missing key -- and a crash in the browser, because the
SPA casts JSON without validating -- into a loud failure right here.
"""

from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.db.schemas.common import Response as ResponseModel
from app.main import app

# A 204 has no body to describe. Nothing else gets a pass.
NO_BODY = {
    ("/api/v1/mail/thread/{thread_id}", "DELETE"),
    ("/api/v1/auth/connections/{connection_id}", "DELETE"),
}


def _routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                yield route, method


def test_every_route_declares_a_response_model():
    missing = [
        f"{method} {route.path}"
        for route, method in _routes()
        if route.response_model is None and (route.path, method) not in NO_BODY
    ]
    assert not missing, f"routes with no response_model: {missing}"


def test_output_models_forbid_extra_fields():
    """The safety net itself. If a model stops forbidding extras, a forgotten
    field goes back to vanishing silently on the way out."""
    lax = []
    for route, _ in _routes():
        model = route.response_model
        if isinstance(model, type) and issubclass(model, BaseModel):
            if model.model_config.get("extra") != "forbid":
                lax.append(f"{route.path} -> {model.__name__}")
    assert not lax, f"response models allowing extra fields: {lax}"


def test_schemas_inherit_the_shared_base():
    """Cheap way to keep the config in one place: if a new model skips the base,
    it also skips extra='forbid' and nobody notices."""
    from app.db.schemas import auth, mailbox

    for module in (auth, mailbox):
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                if obj.__module__ == module.__name__:
                    assert issubclass(obj, ResponseModel), f"{name} should extend the shared base"
