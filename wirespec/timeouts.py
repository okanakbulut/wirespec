"""The two default timeouts, in one place.

Seconds, everywhere, because mixing units is worse than either choice
(§4.3). Playwright's Python API counts milliseconds; the
compatibility surface converts at its boundary rather than here.
"""

__all__ = ["DEFAULT_ACTION_TIMEOUT", "DEFAULT_ASSERTION_TIMEOUT"]

#: How long a retrying assertion waits before quoting the last thing it saw.
DEFAULT_ASSERTION_TIMEOUT = 5.0

#: How long an action waits for the page to let it happen. Longer than an
#: assertion because an action can be waiting on a navigation (§5.1).
DEFAULT_ACTION_TIMEOUT = 15.0
