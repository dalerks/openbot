"""Split a byte stream of back-to-back JSON values (no delimiters), as kaiten sends."""

import json

_OPEN = (ord("{"), ord("["))
_CLOSE = (ord("}"), ord("]"))
_QUOTE = ord('"')
_BACKSLASH = ord("\\")
_WS = (ord(" "), ord("\t"), ord("\r"), ord("\n"))


class JsonStreamError(ValueError):
    pass


def split_value(buf, start=0):
    """Find the first complete JSON object/array in `buf` at or after `start`.

    Returns (value, end_index) or None if the buffer holds only a partial value.
    Leading whitespace is skipped; anything else before a value is an error.
    """
    i = start
    n = len(buf)
    while i < n and buf[i] in _WS:
        i += 1
    if i == n:
        return None
    if buf[i] not in _OPEN:
        raise JsonStreamError(f"unexpected byte {bytes(buf[i:i + 20])!r} in JSON stream")
    begin = i
    depth = 0
    in_str = False
    esc = False
    while i < n:
        b = buf[i]
        if in_str:
            if esc:
                esc = False
            elif b == _BACKSLASH:
                esc = True
            elif b == _QUOTE:
                in_str = False
        elif b == _QUOTE:
            in_str = True
        elif b in _OPEN:
            depth += 1
        elif b in _CLOSE:
            depth -= 1
            if depth == 0:
                return json.loads(bytes(buf[begin:i + 1])), i + 1
        i += 1
    return None
