from .common import Response


class OverviewSummary(Response):
    threads: int
    messages: int
    classified: int


class Overview(Response):
    # Nested, not flat. The SPA reads overview.summary.threads -- flattening this
    # would drop `summary` on the way out and throw in the browser, with a 200 on
    # the wire and nothing in the logs.
    summary: OverviewSummary
