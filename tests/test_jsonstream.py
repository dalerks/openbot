import pytest

from openbot.printer.jsonstream import JsonStreamError, split_value


def test_back_to_back_objects():
    buf = bytearray(b'{"a": 1}{"b": [1, {"c": 2}]}  {"d"')
    v, end = split_value(buf)
    assert v == {"a": 1}
    v, end = split_value(buf, end)
    assert v == {"b": [1, {"c": 2}]}
    assert split_value(buf, end) is None      # partial third object


def test_braces_and_escapes_inside_strings():
    buf = bytearray(rb'{"s": "}{ \" ]"} ')
    v, end = split_value(buf)
    assert v == {"s": '}{ " ]'}


def test_whitespace_only_is_incomplete():
    assert split_value(bytearray(b"  \n ")) is None


def test_garbage_raises():
    with pytest.raises(JsonStreamError):
        split_value(bytearray(b"xyz{}"))
