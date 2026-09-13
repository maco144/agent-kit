"""Message windowing that never separates a tool call from its results."""

from __future__ import annotations


def window_indices(roles: list[str], keep: int) -> list[int]:
    """
    Return the indices of the messages to keep when trimming history to ``keep``.

    ``roles`` are the non-system message roles in order. Providers reject
    history that starts on an assistant turn or on a tool result whose call was
    trimmed, so the window is soft: it grows past ``keep`` rather than emit an
    invalid request.

    - If a user turn falls inside the window, keep from the first one.
    - Otherwise (one long tool-using exchange), keep the latest user turn as an
      anchor, then resume at the first assistant turn that still fits beside it,
      so every kept tool result follows its call. If none fits, resume at the
      latest assistant turn before that point and exceed the window.
    """
    n = len(roles)
    if n <= keep:
        return list(range(n))
    cut = n - keep

    for i in range(cut, n):
        if roles[i] == "user":
            return list(range(i, n))

    last_user = max((i for i in range(cut) if roles[i] == "user"), default=None)
    earliest = cut + (0 if last_user is None else 1)  # the anchor takes one slot
    resume = next((i for i in range(earliest, n) if roles[i] == "assistant"), None)
    if resume is None:
        resume = max((i for i in range(earliest) if roles[i] == "assistant"), default=cut)
    if last_user is None:
        return list(range(resume, n))
    if resume <= last_user:
        return list(range(last_user, n))
    return [last_user, *range(resume, n)]
