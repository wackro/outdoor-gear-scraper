"""Pushing alerts to a human, fast.

`base` defines the payload and the interface; each module beside it is one
channel. Keeping this behind an interface is deliberate — the choice of push
service is the most likely thing to change, and swapping it should not touch the
detection logic.
"""
from .base import Alert, Notifier, build_notifier

__all__ = ["Alert", "Notifier", "build_notifier"]
