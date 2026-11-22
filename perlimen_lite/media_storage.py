"""Filesystem backing for Lite media; never mounted as static files."""

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from shutil import copyfileobj
from typing import BinaryIO

ASSET_ID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
VARIANT = r"(?:avatar_(?:256|512)|post_(?:480|1280))\.webp"
OBJECT_KEY = re.compile(rf"assets/({ASSET_ID})/(original|{VARIANT})")


def _filename(key: str) -> str:
    match = OBJECT_KEY.fullmatch(key)
    if match is None:
        raise KeyError(key)
    return ".".join(match.groups())


class LocalMediaStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    @contextmanager
    def _directory(self) -> Iterator[int]:
        fd = os.open(self.root, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
                raise PermissionError("media directory is not private")
            yield fd
        finally:
            os.close(fd)

    def ready(self) -> bool:
        try:
            with self._directory():
                return os.access(self.root, os.R_OK | os.W_OK | os.X_OK)
        except OSError:
            return False

    def put_object(
        self, *, bucket: str, key: str, body: bytes | BinaryIO, content_type: str
    ) -> None:
        name = _filename(key)
        with self._directory() as directory:
            fd = os.open(name, os.O_WRONLY | os.O_EXCL | os.O_CREAT, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as target:
                if isinstance(body, bytes):
                    target.write(body)
                else:
                    copyfileobj(body, target)
                target.flush()
                os.fsync(target.fileno())
            os.fsync(directory)

    def get_object(self, *, bucket: str, key: str) -> tuple[bytes, str]:
        name = _filename(key)
        with self._directory() as directory:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                raise KeyError(key) from None
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()
                ):
                    raise PermissionError("media blob is not a private file")
                # Lite only serves WebP derivatives, never originals.
                mime = "image/webp" if name.endswith(".webp") else "application/octet-stream"
                return source.read(), mime

    def delete_object(self, *, bucket: str, key: str) -> None:
        name = _filename(key)
        with self._directory() as directory:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)
            os.fsync(directory)
