"""Log manager for PaneldeContolOpenClaw.

This class encapsulates writing execution logs to disk and maintaining
a configured maximum total size.  Logs are stored in the configured
directory under names derived from the run identifier.  When the
cumulative size of all log files exceeds the configured limit, the
oldest logs are deleted automatically.  This prevents uncontrolled
disk consumption.

The log manager is intentionally simple: it does not compress logs
(although this could be added in future) and assumes exclusive
ownership of the log directory.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path


class LogManager:
    """Manages writing and pruning log files.

    Parameters
    ----------
    log_dir : str
        Path to the directory where logs should be stored.  The
        directory will be created if it does not exist.
    max_size_mb : int
        Maximum total size of log files in megabytes.  When the
        cumulative size exceeds this limit the oldest files are
        removed until the total is under the limit again.
    """

    def __init__(self, log_dir: str, max_size_mb: int, compress_days: int = None) -> None:
        """Create a log manager.

        Parameters
        ----------
        log_dir : str
            Directory where logs are stored.
        max_size_mb : int
            Maximum cumulative size of log files in megabytes.
        compress_days : int, optional
            If provided, logs older than this number of days will be
            compressed into gzip files to save space.  Set to None
            to disable compression.  Compressed logs still count
            towards the size limit.
        """
        self.log_dir = Path(log_dir)
        self.max_bytes = max_size_mb * 1024 * 1024
        self.compress_days = compress_days
        self.lock = threading.Lock()
        self.ensure_log_dir()

    def ensure_log_dir(self) -> None:
        """Ensure that the log directory exists."""
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _total_size(self) -> int:
        """Calculate the total size of all files in the log directory."""
        total = 0
        for entry in self.log_dir.iterdir():
            if entry.is_file():
                total += entry.stat().st_size
        return total

    def _prune_if_needed(self) -> None:
        """Remove oldest log files until the total size is below the limit."""
        # Acquire the lock to avoid concurrent deletions/writes.
        with self.lock:
            total = self._total_size()
            if total <= self.max_bytes:
                return
            # Build list of files with their modification times.
            files = [(f.stat().st_mtime, f) for f in self.log_dir.iterdir() if f.is_file()]
            # Sort by modification time ascending (oldest first)
            files.sort()
            for mtime, file_path in files:
                try:
                    size = file_path.stat().st_size
                    file_path.unlink()
                    total -= size
                    if total <= self.max_bytes:
                        break
                except Exception:
                    # Ignore deletion errors but continue processing
                    continue

    def write_log(self, run_id: str, data: str) -> str:
        """Append data to the log file for the given run.

        Parameters
        ----------
        run_id : str
            Identifier of the run; used to name the log file.
        data : str
            Text to append to the log file.

        Returns
        -------
        str
            The absolute path of the log file.
        """
        # Acquire lock around write to prevent race with pruning.
        with self.lock:
            log_path = self.log_dir / f"{run_id}.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(data)
            # Optionally compress old logs before pruning
            self._compress_old_logs()
            # After writing, prune if necessary.
            self._prune_if_needed()
            return str(log_path)

    def _compress_old_logs(self) -> None:
        """Compress log files older than the configured threshold using gzip.

        This method will walk through all files in the log directory and
        compress those that are older than `compress_days`.  It only
        compresses files with the `.log` extension.  Compressed files
        keep the same name with `.gz` appended and the original file
        is removed.  If `compress_days` is None this method does
        nothing.
        """
        if not self.compress_days:
            return
        import gzip
        threshold = time.time() - (self.compress_days * 86400)
        for entry in self.log_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix == ".gz":
                continue
            if not entry.name.endswith(".log"):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime > threshold:
                continue
            gz_path = entry.with_suffix(entry.suffix + ".gz")
            # Compress file
            try:
                with open(entry, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                    f_out.writelines(f_in)
                # Preserve modification time on compressed file
                os.utime(gz_path, (mtime, mtime))
                entry.unlink()
            except Exception:
                # If compression fails, leave original file
                continue

    def get_stats(self) -> dict[str, int]:
        """Return statistics about the current log storage.

        Returns
        -------
        dict[str, int]
            A dictionary with keys `total_bytes` and `file_count`.
        """
        with self.lock:
            total = self._total_size()
            count = sum(1 for f in self.log_dir.iterdir() if f.is_file())
            return {"total_bytes": total, "file_count": count}

    def get_top_files(self, n: int = 5) -> list[tuple[str, int]]:
        """Return the n largest log files sorted descending by size.

        Returns
        -------
        list of tuple (filename, size_bytes)
        """
        files = []
        with self.lock:
            for f in self.log_dir.iterdir():
                if f.is_file():
                    try:
                        size = f.stat().st_size
                        files.append((f.name, size))
                    except OSError:
                        continue
        files.sort(key=lambda x: x[1], reverse=True)
        return files[:n]
