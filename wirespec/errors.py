"""Every exception wirespec raises."""


class WirespecError(Exception):
    """Base class for everything wirespec raises deliberately."""


class LaunchError(WirespecError):
    """Chrome could not be started, or died before it said anything."""


class ConnectionClosedError(WirespecError):
    """The pipe went away while a call was in flight, or before it was made."""


class CDPError(WirespecError):
    """Chrome answered a command with an error object rather than a result.

    ``code`` and ``message`` come straight from the protocol; ``data`` is the
    optional free-text detail Chrome attaches to some failures and is usually
    the part that says what actually went wrong.
    """

    def __init__(self, method: str, code: int, message: str, data: str | None = None) -> None:
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        detail = f"{message}: {data}" if data else message
        super().__init__(f"{method}: {detail} (code {code})")


#: What Chrome says when a node id it once handed out no longer names anything.
#: The **one** CDP error message wirespec matches on, and it earns that: an
#: application that re-renders produces it routinely, between any two of the
#: round trips a read is made of, and it means "resolve again" rather than
#: "something is wrong" (§8.23).
NODE_GONE = "Could not find node with given id"


class NavigationError(WirespecError):
    """A navigation did not arrive.

    Chrome reports a dead host, a refused connection or a bad scheme in
    ``Page.navigate``'s *result*, not as a protocol error, so a driver that
    only catches :class:`CDPError` sails past it and fails somewhere else
    entirely (§8.9).
    """


class WirespecTimeoutError(WirespecError, TimeoutError):
    """Something wirespec waited for did not happen in time.

    Subclasses the builtin ``TimeoutError`` as well, so a spec that already
    writes ``except TimeoutError`` keeps working and nobody has to import a
    name that shadows a builtin. The message carries the last thing wirespec
    saw, which has usually already answered the question that "timed out"
    sends someone to the browser for (§5.1).
    """


class JavaScriptError(WirespecError):
    """The caller's own JavaScript threw, or could not be run.

    A page-side throw is not a protocol error -- the command succeeded and the
    *page* threw -- so Chrome reports it in the result and it would otherwise
    arrive as a ``None`` that fails somewhere else later. ``stack`` is
    JavaScript's own, which is the part worth having in a Python traceback.
    """

    def __init__(self, message: str, *, stack: str | None = None) -> None:
        self.stack = stack
        super().__init__(f"{message}\n{stack}" if stack else message)


class PageClosedError(WirespecError):
    """Something was asked of a page that is gone.

    Chrome answers a command on a dead session with a protocol error naming an
    internal session id and nothing else, and a page closed out from under a
    spec by its own context would otherwise fail that way -- a long distance
    from the line that closed it.
    """
