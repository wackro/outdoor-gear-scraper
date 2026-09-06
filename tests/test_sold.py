"""Deciding a listing has gone.

The failure mode this guards against is severe: the sweep reads only page one, so
a naive "it wasn't in the last response" test would mark most of a busy category
sold. Every one of those would be a lie on the page.
"""
from src.hot.sold import Tracked, detect_gone

# Older timestamp = listed longer ago.
OLD, MID, NEW = 1000, 2000, 3000


def test_absent_but_within_the_pages_range_is_gone():
    # Tracked item is newer than the oldest thing still on the page, so it
    # should have been there. Its absence is real.
    gone = detect_gone([Tracked(1, MID)], returned_ids={2, 3},
                       returned_listed_ts=[OLD, NEW])
    assert gone == [1]


def test_absent_but_older_than_the_whole_page_is_not_gone():
    # It simply fell off page one because newer listings pushed it down. This is
    # the case that a naive check gets wrong.
    gone = detect_gone([Tracked(1, OLD)], returned_ids={2, 3},
                       returned_listed_ts=[MID, NEW])
    assert gone == []


def test_present_is_never_gone():
    assert detect_gone([Tracked(1, NEW)], {1}, [OLD, NEW]) == []


def test_item_without_a_timestamp_is_left_alone():
    # We can't place it in the ordering, and calling a live listing sold is worse
    # than missing a sale.
    assert detect_gone([Tracked(1, None)], {2}, [OLD, NEW]) == []


def test_empty_page_tells_us_nothing():
    assert detect_gone([Tracked(1, NEW)], set(), []) == []


def test_page_with_no_timestamps_tells_us_nothing():
    assert detect_gone([Tracked(1, NEW)], {2}, []) == []


def test_mixed_batch():
    tracked = [Tracked(1, NEW), Tracked(2, MID), Tracked(3, OLD), Tracked(4, None)]
    gone = detect_gone(tracked, returned_ids={9}, returned_listed_ts=[MID])
    assert sorted(gone) == [1]      # only #1 is newer than the page's oldest
