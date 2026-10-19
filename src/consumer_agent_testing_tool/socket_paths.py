"""Short addresses for workspace-owned Unix sockets, without changing directories."""
import os
import sys
from pathlib import Path

from .errors import LabError


def socket_address(path):
    """Call after path admission; this changes the spelling, not the destination."""
    absolute = str(Path(path).resolve())
    relative = os.path.relpath(absolute)
    address = min((absolute, relative), key=lambda value: len(os.fsencode(value)))
    # sockaddr_un includes a terminating NUL: 104 bytes on macOS, 108 on Linux.
    limit = 103 if sys.platform == "darwin" else 107
    if len(os.fsencode(address)) > limit:
        raise LabError("Unix socket path is too long; use a shorter path within the workspace")
    return address
