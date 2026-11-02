"""nexus_lite._proc — reap intentionally detached children. Stdlib only."""

from __future__ import annotations

import subprocess
import threading


def reap_in_background(proc: "subprocess.Popen") -> "subprocess.Popen":
    """Wait for a detached child on a daemon thread so it never lingers as a zombie.

    The child keeps running independently (start_new_session=True) and outlives this
    process; the thread only collects its exit status if we are still alive. The thread
    holds a reference, so no 'subprocess still running' ResourceWarning is emitted.
    """
    threading.Thread(target=proc.wait, name=f"reap-{proc.pid}", daemon=True).start()
    return proc
