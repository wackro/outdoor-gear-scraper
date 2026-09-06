"""Print alerts instead of pushing them — for dry runs and local development."""
from __future__ import annotations

import logging

from .base import Alert, Notifier

log = logging.getLogger(__name__)


class ConsoleNotifier(Notifier):
    def send(self, alert: Alert) -> bool:
        flag = "URGENT" if alert.urgent else "ALERT "
        indented = "\n".join(f"        {line}" for line in alert.body().splitlines())
        print(f"\n[{flag}] {alert.headline()}   heat={alert.heat:.2f}")
        print(indented)
        print(f"        {alert.url}")
        return True
