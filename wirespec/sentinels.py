"""Values that mean "nothing was passed", which is not the same as ``None``.

One module rather than one per user, because a sentinel is only useful if it is
the *same object* everywhere -- two modules each defining their own would
compare unequal and the argument would be silently dropped on the way between
them.
"""

from typing import Any

__all__ = ["NO_ARGUMENT"]

#: No second argument to ``evaluate`` at all. ``None`` cannot serve here: it is
#: JavaScript's ``null``, and a spec passing it means it. With ``None`` as the
#: default the two are indistinguishable, and ``evaluate("x => x === null",
#: None)`` would be answered by a call with no argument.
NO_ARGUMENT: Any = object()
