"""Observe an atomic completion file through Linux inotify without polling."""

import ctypes
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time


def main():
    with tempfile.TemporaryDirectory(prefix="goal-wait-inotify-") as directory:
        root = Path(directory)
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        child = None
        try:
            watch = libc.inotify_add_watch(fd, os.fsencode(root), 0x00000080)
            if watch < 0:
                raise OSError(ctypes.get_errno(), "inotify_add_watch failed")
            started = time.monotonic()
            cpu = time.process_time()
            child = subprocess.Popen(
                [sys.executable, "-c", """
import json, pathlib, sys, time
root = pathlib.Path(sys.argv[1])
time.sleep(2)
(root / 'result.tmp').write_text(json.dumps({'exit_code': 0}))
(root / 'result.tmp').rename(root / 'result.json')
""", str(root)]
            )
            # This read blocks in the kernel until the rename event arrives.
            data = os.read(fd, 4096)
            _, mask, _, size = struct.unpack_from("iIII", data)
            name = data[16:16 + size].split(b"\0", 1)[0].decode()
            child.wait()
            result = json.loads((root / "result.json").read_text())
            assert mask & 0x00000080
            assert name == "result.json"
            assert result["exit_code"] == 0
            print(json.dumps({
                "mechanism": "blocking inotify read; IN_MOVED_TO on directory",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "watcher_cpu_seconds": round(time.process_time() - cpu, 6),
                "event_file": name,
                "result": result,
            }))
        finally:
            os.close(fd)
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait()


if __name__ == "__main__":
    main()
