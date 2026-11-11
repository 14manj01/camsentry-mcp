"""The loop that keeps watching: analyse new stills, then tell the user about the ones that matter.

It runs as a background thread inside the MCP server, so adding the server to a model's client
is all it takes, and as the standalone `camsentry-mcp watch` command for when no client is open.
Several watchers may run at once: the store hands each still and each notification out once.
"""

from __future__ import annotations

import logging
import threading

from .images import ImageError
from .notify import NotifyError
from .service import CameraWatch
from .vision import VisionError
from .wyze import NotLoggedIn, WyzeError

_BATCH = 10
_MAX_CATCH_UP_ROUNDS = 5
_FIRST_CYCLE_DELAY = 2.0
_EXPECTED = (NotLoggedIn, WyzeError, VisionError, ImageError, NotifyError)

log = logging.getLogger("camsentry")


class Watcher(threading.Thread):
    def __init__(self, watch: CameraWatch, interval_seconds: int) -> None:
        super().__init__(name="camsentry-watcher", daemon=True)
        self._watch = watch
        self._interval = interval_seconds
        self._stopping = threading.Event()
        self._last_problem: str | None = None

    def stop(self) -> None:
        self._stopping.set()

    def cycle(self) -> int:
        """One round of watching. Returns the number of notifications sent.

        Problems that fix themselves (signed out, model server down, no network) are logged once
        and retried on the next round instead of ending the watcher.
        """
        try:
            for _ in range(_MAX_CATCH_UP_ROUNDS):
                if self._watch.poll(_BATCH) == 0:
                    break
            sent = self._watch.notify_pending()
        except _EXPECTED as problem:
            if str(problem) != self._last_problem:
                self._last_problem = str(problem)
                log.warning("camera watching is paused: %s", problem)
            return 0
        if self._last_problem is not None:
            self._last_problem = None
            log.warning("camera watching has resumed")
        return sent

    def run(self) -> None:
        delay = _FIRST_CYCLE_DELAY
        while not self._stopping.wait(delay):
            self.cycle()
            delay = self._interval
