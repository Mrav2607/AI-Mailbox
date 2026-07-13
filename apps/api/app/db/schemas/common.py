"""Base class for every response model.

``extra="forbid"`` is doing real work here, not tidiness. FastAPI validates a
route's return value against its response_model and then *drops* whatever the
model doesn't declare -- silently, with a 200. The SPA casts responses straight
to its TypeScript types without validating, so a field we forget to model
doesn't fail a type check, it fails at runtime in the browser as
``cannot read property of undefined``.

Forbidding extras inverts that: a route returning a key its model doesn't know
about raises instead of quietly shedding it, so the mistake surfaces in the test
suite rather than in someone's console.
"""

from pydantic import BaseModel, ConfigDict


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
