"""Playwright's API, over wirespec's driver.

A **translation layer**, not a bag of aliases (§15.2). Playwright
counts milliseconds and wirespec counts seconds; Playwright spells a viewport as
a dict and wirespec as a tuple; Playwright's ``page.on`` is synchronous and
wirespec's is awaited. Both cannot be true of one function, so the native API
keeps its own spelling and the conversion happens here -- in one place, tested,
rather than as a unit ambiguity spread across every call site.

The rule that shapes every class in here is §15.4: **anything
Playwright supports that wirespec does not must raise ``NotImplementedError``
naming the feature.** A compatibility layer whose gaps are silent turns a
missing feature into a wrong result, which is the failure this project keeps
returning to. Each wrapper therefore states its surface explicitly and answers
everything else through ``__getattr__`` with a refusal that names what was
asked for -- so a suite using something unbuilt stops on the line that uses it.
"""
