"""Start and stop background builds with their native tool subprocesses."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def process_group_options() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_process(process: subprocess.Popen | None, *, process_group: bool = False) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # terminate()/kill() only stops the parent on Windows. taskkill /T
            # also stops MFA/ffmpeg descendants that may still hold log files.
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
                timeout=5,
            )
        elif process_group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        try:
            if sys.platform != "win32" and process_group:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()
