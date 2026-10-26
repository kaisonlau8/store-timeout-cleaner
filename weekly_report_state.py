"""周报去重状态：失败关闭、原子持久化、跨进程非阻塞锁。"""

import errno
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def load_state(path):
    path = Path(path)
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or any(
        not isinstance(entry, dict)
        or entry.get("status") not in ("sent", "sending", "dry_run", "failed")
        for entry in state.values()
    ):
        raise ValueError(f"周报状态文件格式错误：{path}")
    return state


def atomic_write(path, writer):
    """同目录唯一临时文件，fsync 后替换并同步目录项。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    temporary = Path(name)
    try:
        writer(temporary)
        with temporary.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def save_state(path, state):
    atomic_write(path, lambda tmp: tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    ))


@contextmanager
def report_lock(state_path):
    path = Path(state_path)
    lock_path = path.parent / "weekly_report.lock"
    if path == lock_path:
        raise ValueError("状态文件不能使用 weekly_report.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 不删除锁文件，避免其他进程锁到不同 inode。
    with lock_path.open("a", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
