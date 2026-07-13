from .common import Response


class Health(Response):
    status: str


class Readiness(Response):
    status: str
    # "ok" or a short error label per dependency -- open-ended by design, since
    # the label carries the exception type.
    checks: dict[str, str]
