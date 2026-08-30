"""``alert``, ``confirm``, ``prompt``: the page asking a question back.

The only thing a page can do that stops the driver rather than the other way
round. While a dialog is open Chrome's renderer is stopped, so the command that
opened it has not returned and nothing sent after it will be answered either --
a driver with no opinion about dialogs does not misbehave, it stops
(§8.20).

So wirespec always answers. With no handler registered the answer is *dismiss*,
which is the reading that changes least: a spec that never mentioned the dialog
did not ask for the destructive branch of a confirm. A handler registered
through ``page.on("dialog", ...)`` gets first refusal and can decide otherwise.
"""

from typing import TYPE_CHECKING

from wirespec.cdp import page as page_domain
from wirespec.errors import WirespecError

if TYPE_CHECKING:
    from wirespec.page import Page

__all__ = ["Dialog"]


class Dialog:
    """One open dialog, and the two ways to close it.

    Answering is a command, so it is awaited -- unlike Playwright's, whose
    ``Dialog`` is the same shape in both its APIs. Everything read off it is a
    plain attribute rather than a method, because it all arrived in the event
    that announced the dialog and there is nothing left to ask Chrome.
    """

    __slots__ = ("_handled", "default_value", "message", "page", "type")

    def __init__(self, page: Page, opening: page_domain.JavascriptDialogOpening) -> None:
        #: The page that asked. Named ``page`` rather than ``_page`` because
        #: Playwright exposes it and a handler shared between tabs needs it.
        self.page = page
        #: ``"alert"``, ``"confirm"``, ``"prompt"`` or ``"beforeunload"``.
        self.type = opening.type
        self.message = opening.message
        #: What a ``prompt`` was pre-filled with, and ``""`` for every other
        #: kind -- Playwright's spelling. Chrome omits the field entirely
        #: rather than sending an empty one, so ``None`` has to be flattened
        #: here or every caller flattens it instead.
        self.default_value = opening.default_prompt or ""
        self._handled = False

    def __repr__(self) -> str:
        return f"<Dialog {self.type} {self.message!r}{'' if self._handled else ' (open)'}>"

    @property
    def handled(self) -> bool:
        """Has this dialog been answered? Read by the page to decide whether
        the default answer is still needed."""
        return self._handled

    async def accept(self, prompt_text: str | None = None) -> None:
        """Press OK. ``prompt_text`` is what to type first, and is ignored by
        every dialog kind except ``prompt``."""
        await self._answer(accept=True, prompt_text=prompt_text)

    async def dismiss(self) -> None:
        """Press Cancel. A dismissed ``prompt`` hands the page ``null``, which
        is a different value from the ``""`` an accepted empty one gives it."""
        await self._answer(accept=False, prompt_text=None)

    async def _answer(self, *, accept: bool, prompt_text: str | None) -> None:
        # Guarded here rather than left to Chrome, because Chrome does not
        # answer *this* dialog -- it answers whichever one is open when the
        # command lands. A second answer for a dialog already closed is either
        # an error about no dialog showing or, far worse, an answer to the next
        # question the page asks.
        if self._handled:
            raise WirespecError(
                f"the {self.type} {self.message!r} has already been answered; a dialog takes exactly one "
                f"accept or dismiss, and a second would answer whatever dialog opens next."
            )
        self._handled = True
        await self.page.session.send(
            # `prompt_text=None` is left out of the message entirely rather
            # than sent as null, so Chrome applies its own default -- which for
            # a prompt is the empty string, not the value it was pre-filled
            # with (measured, Chrome 150).
            page_domain.HandleJavaScriptDialog(accept=accept, prompt_text=prompt_text)
        )
