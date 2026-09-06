"""Locks in the existing size/condition behaviour.

Not new logic — but the alert path now depends on it, so a silent change here
would start pushing notifications for things the user cannot wear.
"""
from src.filters import condition_rank, passes_condition, size_matches


class TestCondition:
    def test_ordering(self):
        assert condition_rank("New with tags") > condition_rank("Good")
        assert condition_rank("Good") > condition_rank("Satisfactory")

    def test_floor_admits_equal_and_better(self):
        assert passes_condition("Good", "Good")
        assert passes_condition("Very good", "Good")
        assert not passes_condition("Satisfactory", "Good")

    def test_unknown_condition_is_excluded(self):
        assert condition_rank("") == 0
        assert not passes_condition("", "Good")

    def test_misconfigured_floor_does_not_hide_everything(self):
        assert passes_condition("Satisfactory", "nonsense")


class TestSizes:
    def test_clothes_token_match(self):
        assert size_matches("clothes", "M", ["M", "L"])
        assert not size_matches("clothes", "XS", ["M", "L"])

    def test_shoes_read_the_uk_number(self):
        assert size_matches("shoes", "UK 9", ["8.5", "9"])
        assert not size_matches("shoes", "39", ["9"])       # EU must not match UK

    def test_trousers_read_the_waist_not_the_leg(self):
        assert size_matches("trousers", "W32 L34", ["32"])
        assert size_matches("trousers", "30/32", ["30"])
        assert not size_matches("trousers", "W34 L32", ["32"])

    def test_empty_allowlist_means_unrestricted(self):
        # This is how bags skip size filtering entirely.
        assert size_matches("bags", "anything", [])
