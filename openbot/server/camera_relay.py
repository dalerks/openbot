"""One camera stream from the printer, fanned out to any number of viewers.

The printer's small ARM board streams once; each viewer gets the latest JPEG
(slow viewers skip frames rather than building a backlog). The stream stops a
few seconds after the last viewer leaves.
"""

import asyncio
import contextlib
import logging

log = logging.getLogger(__name__)


class CameraRelay:
    def __init__(self, backend_getter, idle_stop=5.0):
        self._backend = backend_getter
        self._subscribers: set[asyncio.Queue] = set()
        self._task = None
        self._idle_stop = idle_stop
        self.latest = None

    @contextlib.asynccontextmanager
    async def subscribe(self):
        q = asyncio.Queue(maxsize=1)
        self._subscribers.add(q)
        if self.latest is not None:
            q.put_nowait(self.latest)
        self._ensure_running()
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    def _ensure_running(self):
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    async def _run(self):
        backend = self._backend()
        if backend is None or not backend.supports("camera"):
            return
        idle_since = None
        stream = backend.camera_stream()
        try:
            async for frame in stream:
                if not frame.is_jpeg:
                    continue
                self.latest = frame.data
                for q in list(self._subscribers):
                    if q.full():
                        q.get_nowait()
                    q.put_nowait(frame.data)
                if self._subscribers:
                    idle_since = None
                else:
                    loop = asyncio.get_running_loop()
                    idle_since = idle_since or loop.time()
                    if loop.time() - idle_since > self._idle_stop:
                        break
        except Exception as e:  # noqa: BLE001
            log.info("camera relay stopped: %r", e)
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    async def close(self):
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
