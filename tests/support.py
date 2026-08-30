"""Launching a disposable Chrome for the protocol suite.

Discovery and the command line moved to ``wirespec.browser`` when the driver
was built (§13 step 2); they are re-exported here so the protocol
tests, which predate ``Browser`` and deliberately drive a raw ``Connection``,
keep one import.
"""

import contextlib
import os
from collections.abc import AsyncIterator, Sequence

from wirespec.browser import CANDIDATES, chrome_argv, find_chrome
from wirespec.connection import Connection

__all__ = ["CANDIDATES", "chrome", "chrome_argv", "find_chrome"]


@contextlib.asynccontextmanager
async def chrome(user_data_dir: str, *, extra: Sequence[str] = ()) -> AsyncIterator[Connection]:
    binary = find_chrome()
    assert binary is not None, "no Chrome found"
    stderr_path = os.path.join(user_data_dir, "chrome-stderr.log")
    os.makedirs(user_data_dir, exist_ok=True)
    connection = await Connection.launch(
        chrome_argv(binary, user_data_dir, extra_flags=extra),
        stderr_path=stderr_path,
    )
    try:
        yield connection
    finally:
        await connection.close()
