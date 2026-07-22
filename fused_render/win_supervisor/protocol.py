"""Supervisor IPC wire protocol — port of
windows/supervisor/src/protocol.rs (feat/windows-desktop-foundation, PR
#162). Keep this byte-identical to the Rust version: a secondary launch of
either implementation must be able to talk to a primary instance of the
other during a migration.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

_MAGIC = 0x3153_5246
_VERSION = 1
_HEADER_LEN = 12
_MAX_PATH_UNITS = 32_767


class _Opcode(IntEnum):
    OPEN = 1
    OPEN_HOME = 2
    SHUTDOWN_FOR_UPGRADE = 3
    START_IN_BACKGROUND = 4


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Open:
    path: str


@dataclass(frozen=True)
class OpenHome:
    pass


@dataclass(frozen=True)
class StartInBackground:
    pass


@dataclass(frozen=True)
class ShutdownForUpgrade:
    pass


Command = Open | OpenHome | StartInBackground | ShutdownForUpgrade


def encode(command: Command) -> bytes:
    if isinstance(command, Open):
        opcode, payload = _Opcode.OPEN, command.path
    elif isinstance(command, OpenHome):
        opcode, payload = _Opcode.OPEN_HOME, ""
    elif isinstance(command, ShutdownForUpgrade):
        opcode, payload = _Opcode.SHUTDOWN_FOR_UPGRADE, ""
    elif isinstance(command, StartInBackground):
        opcode, payload = _Opcode.START_IN_BACKGROUND, ""
    else:
        raise TypeError(f"unknown command: {command!r}")

    units = payload.encode("utf-16-le")
    n_units = len(units) // 2
    header = struct.pack("<IHHI", _MAGIC, _VERSION, int(opcode), n_units)
    return header + units


def decode(frame: bytes) -> Command:
    if len(frame) < _HEADER_LEN:
        raise ProtocolError("truncated command header")
    magic, version, opcode, n_units = struct.unpack("<IHHI", frame[:_HEADER_LEN])
    if magic != _MAGIC or version != _VERSION:
        raise ProtocolError("unsupported command protocol")
    if n_units > _MAX_PATH_UNITS or len(frame) != _HEADER_LEN + n_units * 2:
        raise ProtocolError("invalid command payload length")

    try:
        payload = frame[_HEADER_LEN:].decode("utf-16-le")
    except UnicodeDecodeError as error:
        raise ProtocolError("invalid command payload encoding") from error
    if opcode == _Opcode.OPEN and payload:
        return Open(payload)
    if opcode == _Opcode.OPEN_HOME and not payload:
        return OpenHome()
    if opcode == _Opcode.SHUTDOWN_FOR_UPGRADE and not payload:
        return ShutdownForUpgrade()
    if opcode == _Opcode.START_IN_BACKGROUND and not payload:
        return StartInBackground()
    raise ProtocolError("invalid command opcode or payload")


def parse_args(args: list[str]) -> Command:
    if not args:
        return OpenHome()
    if len(args) > 1:
        raise ProtocolError(
            "expected one file path, --startup, or --shutdown-for-upgrade"
        )
    first = args[0]
    if first == "--startup":
        return StartInBackground()
    if first == "--shutdown-for-upgrade":
        return ShutdownForUpgrade()
    return Open(first)
