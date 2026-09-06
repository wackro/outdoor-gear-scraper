"""ntfy.sh push notifications.

Uses ntfy's JSON publishing format rather than its HTTP-header format. The header
form cannot carry non-ASCII without manual encoding, and this watchlist is full
of it — Fjällräven, Norrøna, Klättermusen, Haglöfs, Páramo, and a £ sign in every
single alert. JSON sidesteps the whole problem.

stdlib `urllib` on purpose: the repo's only HTTP dependency is `curl_cffi`, which
exists to defeat Vinted's TLS fingerprinting. ntfy needs none of that, and using
it here would mean a browser-impersonation session for a plain API call.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from .base import Alert, Notifier

log = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT_SEC = 10

# ntfy priorities: 1 min, 3 default, 4 high, 5 max. 5 breaks through a phone's
# silent mode, so it is reserved for the strongest signal we have — a listing
# that is both moving fast AND clearly underpriced.
PRIORITY_HIGH = 4
PRIORITY_URGENT = 5


class NtfyNotifier(Notifier):
    def __init__(
        self,
        topic: str,
        *,
        server: str = DEFAULT_SERVER,
        token: str | None = None,
    ):
        if not topic:
            raise ValueError("ntfy topic must not be empty.")
        self.topic = topic
        self.server = (server or DEFAULT_SERVER).rstrip("/")
        # Token via env, never config: it belongs in a repo secret. It also moves
        # rate limiting from the shared runner IP to your account, which matters
        # on Actions where the IP is shared with every other GitHub job.
        self.token = token or os.environ.get("NTFY_TOKEN") or None

    def _payload(self, alert: Alert) -> dict:
        payload = {
            "topic": self.topic,
            "title": alert.headline(),
            "message": alert.body(),
            "priority": PRIORITY_URGENT if alert.urgent else PRIORITY_HIGH,
            "tags": ["fire"] if alert.urgent else ["eyes"],
        }
        if alert.url:
            # The single most important field: the whole point is to get from
            # buzz-in-pocket to the listing in one tap.
            payload["click"] = alert.url
            payload["actions"] = [
                {"action": "view", "label": "Open listing", "url": alert.url}
            ]
        if alert.image_url:
            # An external URL, so it costs nothing against ntfy's attachment quota.
            payload["attach"] = alert.image_url
        return payload

    def send(self, alert: Alert) -> bool:
        """Publish one alert. Never raises — a failed push must not stop polling."""
        body = json.dumps(self._payload(alert)).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(self.server, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
                if 200 <= response.status < 300:
                    log.info("Alerted: %s", alert.headline())
                    return True
                log.warning("ntfy returned HTTP %s", response.status)
                return False
        except urllib.error.HTTPError as exc:
            # 429 here means the topic's rate limit is exhausted (60 message
            # burst, refilling one per 5s on the free tier). Our own burst cap
            # should normally keep us well clear of it.
            log.warning("ntfy HTTP %s: %s", exc.code, exc.reason)
            return False
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            log.warning("ntfy send failed: %s", exc)
            return False
