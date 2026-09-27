"""Turn the library's dataclasses into JSON for the server/client protocol and back."""

import dataclasses

from ..printer import models

# Types that may cross the wire. Anything else must already be plain JSON.
_TYPES = {cls.__name__: cls for cls in (
    models.PrinterStatus, models.Extruder, models.Process, models.NetworkState,
    models.WifiNetwork, models.StaticIpConfig, models.PrinterInfo)}


def to_wire(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        name = type(obj).__name__
        if name not in _TYPES:
            raise TypeError(f"can't send {name} over the wire")
        out = {"__type__": name}
        for f in dataclasses.fields(obj):
            out[f.name] = to_wire(getattr(obj, f.name))
        return out
    if isinstance(obj, (list, tuple)):
        return [to_wire(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): to_wire(v) for k, v in obj.items()}
    if isinstance(obj, (bytes, bytearray)):
        raise TypeError("binary data goes over HTTP, not the JSON channel")
    return obj


def from_wire(obj):
    if isinstance(obj, dict):
        name = obj.get("__type__")
        if name is not None:
            cls = _TYPES.get(name)
            if cls is None:
                raise ValueError(f"unknown wire type {name!r}")
            names = {f.name for f in dataclasses.fields(cls)}
            kwargs = {k: from_wire(v) for k, v in obj.items() if k in names}
            if name == "PrinterStatus" and isinstance(kwargs.get("bed"), list):
                kwargs["bed"] = tuple(kwargs["bed"])
            return cls(**kwargs)
        return {k: from_wire(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_wire(v) for v in obj]
    return obj
