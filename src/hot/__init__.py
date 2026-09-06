"""The hot path: fast polling, attention-velocity scoring and instant alerts.

Separate from the daily pipeline in `src.run` on purpose. The cold path owns the
database, the baselines and the website; the hot path only reads the baselines
and writes to a small, disposable state file. Nothing here commits to git.
"""
