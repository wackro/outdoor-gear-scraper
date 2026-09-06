"""The alert payload and the notifier interface."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass

log = logging.getLogger(__name__)

CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}


@dataclass(frozen=True)
class Alert:
    """One listing worth interrupting someone for."""
    item_id: int
    title: str
    brand_title: str
    price: float
    currency: str
    url: str
    image_url: str
    size: str
    condition: str

    heat: float                     # 0..1 combined attention score
    favourites: int | None
    views: int | None
    fav_per_hour: float | None
    view_per_hour: float | None
    age_minutes: float
    measured: bool                  # False if velocity was inferred, not observed

    baseline: float | None = None   # reference price, when the bracket has one
    discount_pct: float | None = None
    urgent: bool = False            # hot AND clearly underpriced

    @property
    def symbol(self) -> str:
        return CURRENCY_SYMBOLS.get(self.currency, "")

    def headline(self) -> str:
        """One line that has to carry the decision on a lock screen."""
        brand = self.brand_title or "?"
        price = f"{self.symbol}{self.price:.0f}"
        if self.discount_pct:
            return f"{price} {brand} — {self.discount_pct * 100:.0f}% under baseline"
        return f"{price} {brand}"

    def body(self) -> str:
        """Why this fired, in the order that matters when you're deciding fast."""
        lines: list[str] = []

        pace = []
        if self.fav_per_hour is not None:
            likes = f"{self.favourites} like{'' if self.favourites == 1 else 's'}"
            pace.append(f"{likes} ({self.fav_per_hour:.0f}/hr)")
        if self.view_per_hour is not None:
            pace.append(f"{self.views} views ({self.view_per_hour:.0f}/hr)")
        if pace:
            lines.append(" · ".join(pace))

        lines.append(f"Listed {_age(self.age_minutes)} ago")

        detail = [d for d in (self.size, self.condition) if d]
        if detail:
            lines.append(" · ".join(detail))

        if self.baseline:
            lines.append(f"Baseline {self.symbol}{self.baseline:.0f}")
        if not self.measured:
            # Be honest on the notification itself: this one is an estimate from
            # a single sighting, not a measured rate.
            lines.append("(estimated from listing age)")

        if self.title:
            lines.append(self.title[:80])
        return "\n".join(lines)


def _age(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.1f}h"


class Notifier(ABC):
    """A push channel. Implementations must never raise into the poll loop."""

    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """Deliver one alert. Return True on success. Must not raise."""


class BurstLimited(Notifier):
    """Wraps a notifier with a hard ceiling on pushes per hour.

    A scoring bug or a sudden flood of cheap listings should cost you a few
    notifications, not five hundred. Once the cap is hit we drop and log rather
    than queue — a bargain alert delivered an hour late is worthless anyway, so
    holding them would only guarantee a burst of stale pushes later.
    """

    def __init__(self, inner: Notifier, cap_per_hour: int = 10):
        self.inner = inner
        self.cap_per_hour = cap_per_hour
        self._sent: deque[float] = deque()

    def send(self, alert: Alert) -> bool:
        now = time.time()
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()
        if len(self._sent) >= self.cap_per_hour:
            log.warning(
                "Burst cap reached (%d/hour) — dropping alert for item %s (%s). "
                "Raise alerts.burst_cap_per_hour if this is expected.",
                self.cap_per_hour, alert.item_id, alert.headline(),
            )
            return False
        if self.inner.send(alert):
            self._sent.append(now)
            return True
        return False


def build_notifier(config, *, dry_run: bool = False) -> Notifier:
    """Pick a channel from config. `dry_run` always wins, so local runs are safe."""
    from .console import ConsoleNotifier
    from .ntfy import NtfyNotifier

    alerts = config.alerts
    if dry_run or not alerts.enabled or alerts.channel == "console":
        inner: Notifier = ConsoleNotifier()
    elif alerts.channel == "ntfy":
        inner = NtfyNotifier(topic=alerts.ntfy_topic, server=alerts.ntfy_server)
    else:
        raise ValueError(
            f"Unknown alerts.channel {alerts.channel!r} (expected 'ntfy' or 'console')."
        )
    return BurstLimited(inner, cap_per_hour=alerts.burst_cap_per_hour)
