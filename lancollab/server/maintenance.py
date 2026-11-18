"""Cross-process exclusion between the running server and account deletion."""

from contextlib import contextmanager
from pathlib import Path
import os


@contextmanager
def database_maintenance_lock(db_path):
    path = Path(str(Path(db_path).resolve()) + ".server.lock")
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ValueError(
                    "This database is in use. Stop its server before deleting users."
                ) from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ValueError(
                    "This database is in use. Stop its server before deleting users."
                ) from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    # Keep the lock file: unlinking would let processes lock different inodes.
