"""Verified content-addressed artifact publication."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .codec import MAX_JSON_BYTES
from .errors import IntegrityError, PersistenceError
from .models import ArtifactRef

_LOCKS_GUARD = threading.Lock()
_STORE_LOCKS: dict[str, threading.RLock] = {}


@dataclass(frozen=True, slots=True)
class ArtifactRead:
    """Bytes returned by one successful artifact read and its measured size."""

    data: bytes
    bytes_read: int


class ArtifactStore:
    """Owns local bytes addressed by SHA-256 below one configured directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = str(self._root.resolve())
        with _LOCKS_GUARD:
            self._lock = _STORE_LOCKS.setdefault(key, threading.RLock())

    @contextmanager
    def synchronized(self) -> Iterator[None]:
        """Serialize publication, protection snapshots, and eviction per store."""
        with self._lock:
            yield

    @property
    def root(self) -> Path:
        """Return the configured store directory for connector-owned metadata."""
        return self._root

    def put(self, data: bytes, *, media_type: str, provenance: str) -> ArtifactRef:
        """Atomically publish bytes after hashing and verifying the final object."""
        with self._lock:
            return self._put_unlocked(
                data, media_type=media_type, provenance=provenance
            )

    def _put_unlocked(
        self, data: bytes, *, media_type: str, provenance: str
    ) -> ArtifactRef:
        """Publish one object while the store lock is held."""
        if not isinstance(data, bytes):
            raise PersistenceError("artifact data must be bytes")
        if len(data) > MAX_JSON_BYTES:
            raise PersistenceError("artifact exceeds the supported byte bound")
        digest = hashlib.sha256(data).hexdigest()
        target = self._path_for(digest)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists():
            self._verify(target, digest, len(data))
            return ArtifactRef(digest, len(data), media_type, provenance)
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".pending-", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._verify(temporary, digest, len(data))
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    self._verify(target, digest, len(data))
                finally:
                    temporary.unlink(missing_ok=True)
                self._sync_directory(target.parent)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise PersistenceError(
                f"could not publish artifact {digest}: {exc}"
            ) from exc
        return ArtifactRef(digest, len(data), media_type, provenance)

    def register_journal(self, path: str | Path) -> None:
        """Remember a journal that may protect objects in this store."""
        with self._lock:
            resolved = Path(path).resolve()
            known = self.registered_journals()
            if resolved in known:
                return
            index = self._journal_index_path()
            with index.open("a", encoding="utf-8") as stream:
                stream.write(f"{self._stored_journal_path(resolved)}\n")
                stream.flush()
                os.fsync(stream.fileno())

    def registered_journals(self) -> tuple[Path, ...]:
        """Return journal files registered against this store."""
        with self._lock:
            index = self._journal_index_path()
            if not index.is_file():
                return ()
            paths: list[Path] = []
            root = self._root.resolve()
            for line in index.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                stored = Path(text)
                paths.append(
                    stored if stored.is_absolute() else (root / stored).resolve()
                )
            return tuple(dict.fromkeys(paths))

    def _stored_journal_path(self, resolved: Path) -> str:
        try:
            return Path(os.path.relpath(resolved, self._root.resolve())).as_posix()
        except ValueError:
            return str(resolved)

    def _journal_index_path(self) -> Path:
        return self._root / ".journals"

    def inventory(self) -> tuple[tuple[str, int], ...]:
        """Return stored digest/size pairs, smallest path first."""
        with self._lock:
            items: list[tuple[str, int]] = []
            if not self._root.is_dir():
                return ()
            for path in sorted(self._root.rglob("*")):
                if not path.is_file() or path.name.startswith(".pending-"):
                    continue
                digest = path.parent.name + path.name
                if len(digest) != 64 or any(
                    char not in "0123456789abcdef" for char in digest
                ):
                    continue
                items.append((digest, path.stat().st_size))
            return tuple(items)

    def drop(self, digest: str) -> bool:
        """Remove one object. Missing objects are already unavailable."""
        with self._lock:
            path = self._path_for(digest)
            if not path.is_file():
                return False
            try:
                path.unlink()
            except OSError as exc:
                raise PersistenceError(
                    f"could not drop artifact {digest}: {exc}"
                ) from exc
            return True

    def available(self, digest: str) -> bool:
        """Report whether an object currently exists, under the store lock."""
        with self._lock:
            return self._path_for(digest).is_file()

    def read(self, reference: ArtifactRef) -> bytes:
        """Read and verify a required artifact before returning its bytes."""
        with self._lock:
            return self._read_unlocked(reference)

    def read_measured(self, reference: ArtifactRef) -> ArtifactRead:
        """Read and verify bytes, returning the successful read's byte count."""
        with self._lock:
            data = self._read_unlocked(reference)
        return ArtifactRead(data=data, bytes_read=len(data))

    def _read_unlocked(self, reference: ArtifactRef) -> bytes:
        """Read one artifact while the store lock is held."""
        path = self._path_for(reference.digest)
        if not path.is_file():
            raise IntegrityError(f"required artifact is missing: {reference.digest}")
        return self._read_verified(path, reference.digest, reference.size)

    def _path_for(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise IntegrityError("invalid artifact digest")
        return self._root / digest[:2] / digest[2:]

    @staticmethod
    def _verify(path: Path, digest: str, size: int) -> None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PersistenceError(f"could not verify artifact {path}: {exc}") from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest or len(data) != size:
            raise IntegrityError(f"artifact verification failed: {path}")

    @staticmethod
    def _read_verified(path: Path, digest: str, size: int) -> bytes:
        """Hash the exact bytes returned, eliminating verify/read TOCTOU."""
        try:
            if size > MAX_JSON_BYTES:
                raise PersistenceError(
                    f"artifact exceeds the supported byte bound: {digest}"
                )
            if path.stat().st_size > MAX_JSON_BYTES:
                raise PersistenceError(
                    f"artifact exceeds the supported byte bound: {digest}"
                )
            data = path.read_bytes()
        except OSError as exc:
            raise PersistenceError(f"could not read artifact {digest}: {exc}") from exc
        if len(data) > MAX_JSON_BYTES:
            raise PersistenceError(
                f"artifact exceeds the supported byte bound: {digest}"
            )
        if hashlib.sha256(data).hexdigest() != digest or len(data) != size:
            raise IntegrityError(f"artifact verification failed: {path}")
        return data

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
