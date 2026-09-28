"""Process settings that the service applies itself (HF-NAV-010).

The service has no filesystem restriction of its own: Linux permissions of its
account decide every access. It only sets the terminal-equivalent umask.
"""

from __future__ import annotations

import os


SERVICE_UMASK = 0o002


def apply_service_umask() -> None:
    """Use the unit's UMask=0002 so new objects match the account's terminal.

    Per-instance secrets and state do not depend on it: they are created with
    explicit owner-only modes (HF-NAV-010).
    """
    os.umask(SERVICE_UMASK)
