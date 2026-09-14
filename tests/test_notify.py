"""Alert formatting and the burst cap."""
from src.notify.base import Alert, BurstLimited, Notifier
from src.notify.ntfy import PRIORITY_HIGH, PRIORITY_URGENT, NtfyNotifier


def alert(**overrides):
    fields = dict(
        item_id=1, title="Beta AR jacket", brand_title="Fjällräven", price=38.0,
        currency="GBP", url="https://vinted.co.uk/items/1", image_url="http://img",
        size="M", condition="Very good", heat=0.97, favourites=12, views=340,
        fav_per_hour=80.0, view_per_hour=1400.0, age_minutes=9.0, measured=True,
        baseline=150.0, discount_pct=0.747, urgent=True,
    )
    fields.update(overrides)
    return Alert(**fields)


class TestFormatting:
    def test_headline_leads_with_price_and_brand(self):
        assert alert().headline() == "£38 Fjällräven — 75% under baseline"

    def test_headline_without_a_baseline(self):
        assert alert(baseline=None, discount_pct=None).headline() == "£38 Fjällräven"

    def test_body_leads_with_the_reason_it_fired(self):
        assert alert().body().splitlines()[0] == "12 likes (80/hr) · 340 views (1400/hr)"

    def test_singular_like(self):
        assert "1 like (" in alert(favourites=1).body()

    def test_estimates_are_labelled_as_such(self):
        assert "(estimated from listing age)" in alert(measured=False).body()
        assert "(estimated" not in alert(measured=True).body()

    def test_age_switches_to_hours(self):
        assert "Listed 9m ago" in alert().body()
        assert "Listed 2.0h ago" in alert(age_minutes=120).body()


class TestNtfyPayload:
    def test_carries_a_tap_through_to_the_listing(self):
        payload = NtfyNotifier("topic")._payload(alert())
        assert payload["click"] == "https://vinted.co.uk/items/1"
        assert payload["actions"][0]["url"] == "https://vinted.co.uk/items/1"
        assert payload["attach"] == "http://img"

    def test_non_ascii_survives_intact(self):
        # The reason for using the JSON format over HTTP headers: this watchlist
        # is full of Fjällräven, Norrøna, Klättermusen and a £ in every alert.
        assert "Fjällräven" in NtfyNotifier("t")._payload(alert())["title"]

    def test_urgent_escalates_priority(self):
        assert NtfyNotifier("t")._payload(alert())["priority"] == PRIORITY_URGENT
        assert NtfyNotifier("t")._payload(
            alert(urgent=False))["priority"] == PRIORITY_HIGH

    def test_empty_topic_is_rejected_early(self):
        try:
            NtfyNotifier("")
        except ValueError:
            return
        raise AssertionError("expected ValueError for an empty topic")


class Recorder(Notifier):
    def __init__(self, ok=True):
        self.sent = []
        self.health = []
        self.ok = ok

    def send(self, a):
        self.sent.append(a)
        return self.ok

    def send_health(self, title, message):
        self.health.append((title, message))
        return self.ok


class TestBurstCap:
    def test_allows_up_to_the_cap(self):
        inner = Recorder()
        limiter = BurstLimited(inner, cap_per_hour=3)
        assert [limiter.send(alert(item_id=i)) for i in range(3)] == [True] * 3

    def test_drops_beyond_the_cap(self):
        # A scoring bug should cost a few notifications, not five hundred.
        inner = Recorder()
        limiter = BurstLimited(inner, cap_per_hour=2)
        for i in range(5):
            limiter.send(alert(item_id=i))
        assert len(inner.sent) == 2

    def test_a_failed_send_does_not_consume_the_budget(self):
        inner = Recorder(ok=False)
        limiter = BurstLimited(inner, cap_per_hour=2)
        for i in range(4):
            limiter.send(alert(item_id=i))
        assert len(inner.sent) == 4


class TestHealthMessages:
    """Warnings about the poller itself, not about a listing.

    The poller once ran for 5h45m reporting success while tracking nothing at
    all, and nothing said so. These are the rules that make that audible without
    making it annoying.
    """

    def test_it_reaches_the_channel(self):
        inner = Recorder()
        BurstLimited(inner, cap_per_hour=10).send_health("Poller is blind", "0 of 340 fetches worked")
        assert inner.health == [("Poller is blind", "0 of 340 fetches worked")]

    def test_it_does_not_decide_how_often_to_warn(self):
        """That belongs to the caller -- this object has no idea what a run is.

        The poller holds the once-per-run latch, because "a run" is a poller
        concept. Duplicating it here would put the same rule in two layers.
        """
        inner = Recorder()
        notifier = BurstLimited(inner, cap_per_hour=10)
        for _ in range(5):
            assert notifier.send_health("t", "m") is True
        assert len(inner.health) == 5

    def test_it_is_not_paid_for_out_of_the_alert_budget(self):
        """Being flooded is exactly when you need to hear about it."""
        inner = Recorder()
        notifier = BurstLimited(inner, cap_per_hour=2)
        for _ in range(5):
            notifier.send(alert())
        assert len(inner.sent) == 2, "cap should have bitten"

        assert notifier.send_health("still here", "m") is True
        assert len(inner.health) == 1

    def test_an_alert_is_still_allowed_after_a_health_push(self):
        inner = Recorder()
        notifier = BurstLimited(inner, cap_per_hour=2)
        notifier.send_health("t", "m")
        assert notifier.send(alert()) is True
        assert len(inner.sent) == 1


class TestHealthPayload:
    def test_it_is_quieter_than_a_bargain_alert(self):
        """Same urgency as a real find would teach you to ignore both."""
        from src.notify.ntfy import PRIORITY_DEFAULT
        sent = {}
        notifier = NtfyNotifier(topic="t")
        notifier._publish = lambda payload, **kw: sent.update(payload) or True

        notifier.send_health("Poller is blind", "0 of 340 fetches worked")
        assert sent["priority"] == PRIORITY_DEFAULT
        assert sent["priority"] < PRIORITY_HIGH
        assert sent["title"] == "Poller is blind"
        assert "click" not in sent, "there is no listing to open"
