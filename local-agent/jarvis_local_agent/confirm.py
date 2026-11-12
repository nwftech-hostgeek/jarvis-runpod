from __future__ import annotations

import sys
import threading
from typing import Callable


def confirm_console(
    title: str,
    message: str,
    *,
    timeout_seconds: int = 120,
) -> bool:
    """Prompt on the local console. Returns False on timeout or deny."""
    print()
    print("=" * 60)
    print(f"JARVIS LOCAL AGENT CONFIRMATION: {title}")
    print("-" * 60)
    print(message)
    print("-" * 60)
    print(f"Approve? [y/N] (timeout {timeout_seconds}s)")
    sys.stdout.flush()

    result: dict[str, bool] = {"value": False}
    done = threading.Event()

    def _read() -> None:
        try:
            line = sys.stdin.readline()
            result["value"] = line.strip().lower() in ("y", "yes")
        except Exception:
            result["value"] = False
        finally:
            done.set()

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    if not done.wait(timeout_seconds):
        print("Confirmation timed out — denying.")
        return False
    return result["value"]


ConfirmFn = Callable[[str, str], bool]
