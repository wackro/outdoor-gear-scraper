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
        self.ok = ok

    def send(self, a):
        self.sent.append(a)
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
