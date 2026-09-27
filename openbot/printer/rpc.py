"""Async JSON-RPC 2.0 connection to kaiten (TCP :9999 or TLS :12309).

Wire format quirks handled here:
* messages are bare JSON values back to back, no length prefix or newline;
* a `camera_frame` notification is immediately followed by a 16-byte
  big-endian header [total_len incl. header, width, height, format] and the
  raw frame bytes, which must be consumed before JSON parsing resumes;
* `put_raw` is answered first, then the caller streams raw bytes (send_raw).
"""

import asyncio
import itertools
import json
import logging

from .errors import PrinterError, rpc_error_from
from .jsonstream import split_value
from .models import CameraFrame

log = logging.getLogger(__name__)

CAMERA_HEADER_LEN = 16


class RpcConnection:
    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer
        self._buf = bytearray()
        self._ids = itertools.count(1)
        self._pending = {}
        self._listeners = []
        self._frame_listeners = []
        self._close_listeners = []
        self._frame_header = None      # (w, h, fmt, payload_len) while reading a frame
        self._awaiting_frame = False
        self._closed = False
        self._task = asyncio.create_task(self._read_loop())

    @classmethod
    async def open(cls, host, port, ssl=None, timeout=10.0):
        kwargs = {}
        if ssl is not None:
            kwargs = {"ssl": ssl, "server_hostname": host}
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, **kwargs), timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise PrinterError(f"cannot connect to {host}:{port}: {e!r}") from e
        return cls(reader, writer)

    # ---------------------------------------------------------------- info

    @property
    def closed(self):
        return self._closed

    @property
    def peer_certificate_der(self):
        sslobj = self._writer.get_extra_info("ssl_object")
        return sslobj.getpeercert(binary_form=True) if sslobj else None

    @property
    def tls_info(self):
        sslobj = self._writer.get_extra_info("ssl_object")
        if not sslobj:
            return None
        return {"version": sslobj.version(), "cipher": sslobj.cipher()[0]}

    # ---------------------------------------------------------------- listeners

    def on_notification(self, callback):
        """callback(method, params) for every notification. Returns an unsubscribe fn."""
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def on_camera_frame(self, callback):
        """callback(CameraFrame) for every camera frame. Returns an unsubscribe fn."""
        self._frame_listeners.append(callback)
        return lambda: self._frame_listeners.remove(callback)

    def on_close(self, callback):
        self._close_listeners.append(callback)

    # ---------------------------------------------------------------- sending

    async def call(self, method, params=None, timeout=15.0):
        if self._closed:
            raise PrinterError("connection is closed")
        req_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._write({"jsonrpc": "2.0", "method": method,
                           "params": params or {}, "id": req_id})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise PrinterError(f"{method}: no reply within {timeout:.0f}s") from None
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method, params=None):
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def send_raw(self, data):
        self._writer.write(data)
        await self._writer.drain()

    async def _write(self, payload):
        log.debug(">> %s", payload.get("method"))
        self._writer.write(json.dumps(payload).encode())
        await self._writer.drain()

    # ---------------------------------------------------------------- reading

    async def _read_loop(self):
        error = None
        try:
            while True:
                chunk = await self._reader.read(65536)
                if not chunk:
                    break
                self._buf += chunk
                self._drain_buffer()
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - surface any parse/IO error to callers
            error = e
            log.warning("printer connection read error: %r", e)
        finally:
            self._shutdown(error)

    def _drain_buffer(self):
        pos = 0
        while True:
            if self._awaiting_frame:
                pos, complete = self._consume_frame(pos)
                if not complete:
                    break
                continue
            found = split_value(self._buf, pos)
            if found is None:
                break
            msg, pos = found
            self._dispatch(msg)
        del self._buf[:pos]

    def _consume_frame(self, pos):
        """Returns (new_pos, complete). Header bytes are consumed as soon as they arrive."""
        if self._frame_header is None:
            if len(self._buf) - pos < CAMERA_HEADER_LEN:
                return pos, False
            h = self._buf[pos:pos + CAMERA_HEADER_LEN]
            total = int.from_bytes(h[0:4], "big")
            self._frame_header = (int.from_bytes(h[4:8], "big"),
                                  int.from_bytes(h[8:12], "big"),
                                  int.from_bytes(h[12:16], "big"),
                                  max(total - CAMERA_HEADER_LEN, 0))
            pos += CAMERA_HEADER_LEN
        w, h, fmt, size = self._frame_header
        if len(self._buf) - pos < size:
            return pos, False
        frame = CameraFrame(w, h, fmt, bytes(self._buf[pos:pos + size]))
        self._frame_header = None
        self._awaiting_frame = False
        for cb in list(self._frame_listeners):
            _safe(cb, frame)
        return pos + size, True

    def _dispatch(self, msg):
        if not isinstance(msg, dict):
            return
        if "id" in msg and msg.get("id") is not None and "method" not in msg:
            fut = self._pending.get(msg["id"])
            if fut and not fut.done():
                if msg.get("error"):
                    fut.set_exception(rpc_error_from(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
            return
        method = msg.get("method")
        if method == "camera_frame":
            self._awaiting_frame = True
        log.debug("<< notification %s", method)
        for cb in list(self._listeners):
            _safe(cb, method, msg.get("params"))

    # ---------------------------------------------------------------- closing

    def _shutdown(self, error=None):
        if self._closed:
            return
        self._closed = True
        exc = PrinterError(f"connection closed{f': {error!r}' if error else ''}")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass
        for cb in list(self._close_listeners):
            _safe(cb, error)

    async def close(self):
        if not self._closed:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._shutdown()
        try:
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


def _safe(cb, *args):
    try:
        cb(*args)
    except Exception:  # noqa: BLE001 - one bad listener must not kill the connection
        log.exception("listener %r failed", cb)
