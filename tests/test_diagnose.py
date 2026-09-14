"""Diagnosing why the API is refusing us.

The probe exists because `_request` throws the response body away, so when the
catalog endpoint started returning 404 there was no way to tell a moved endpoint
from a bot wall from a rejected parameter -- three problems with three different
fixes and one identical symptom. What is tested here is that it tells them apart.

Offline throughout: the session is a stub, because the suite has no network and
a diagnostic that only works against the live site is not testable at all.
"""
import pytest

from src.vinted.diagnose import Probe, diagnose, interpret, run_probe


class FakeResponse:
    def __init__(self, status, *, body="", headers=None, url="https://x/api/v2/catalog/items"):
        self.status_code = status
        self.text = body
        self.headers = headers or {}
        self.url = url

    def json(self):
        import json
        return json.loads(self.text)


class FakeSession:
    """Answers by matching the path against a rule table."""
    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params or {}))
        for fragment, response in self.rules:
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response
        return FakeResponse(200, body='{"items": []}', url=url)


class FakeClient:
    def __init__(self, session):
        self._session = session
        self.base_url = "https://www.vinted.co.uk"

        class _Config:
            currency = "GBP"

        self.config = _Config()

    def _ensure_session(self):
        return self._session


def probes_from(rules):
    client = FakeClient(FakeSession(rules))
    return diagnose(client, catalog_id=2052, brand_ids=[1, 2, 3],
                    catalog_path="/api/v2/catalog/items", brands_path="/api/v2/brands")


class TestRunProbe:
    def test_it_records_status_body_and_url(self):
        client = FakeClient(FakeSession([("/api/v2/catalog", FakeResponse(404, body="not found"))]))
        probe = run_probe(client, "n", "a", "/api/v2/catalog/items")
        assert probe.status == 404
        assert probe.ok is False
        assert probe.body == "not found"
        assert probe.verdict == "HTTP 404"

    def test_it_counts_items_when_the_shape_is_familiar(self):
        client = FakeClient(FakeSession([("/api", FakeResponse(200, body='{"items":[{},{}]}'))]))
        probe = run_probe(client, "n", "a", "/api/v2/catalog/items")
        assert probe.item_count == 2
        assert probe.verdict == "OK, 2 items"

    def test_a_request_that_never_lands_is_a_result_not_a_crash(self):
        """The probe must survive anything; an exception here is a finding."""
        client = FakeClient(FakeSession([("/api", ConnectionError("tunnel refused"))]))
        probe = run_probe(client, "n", "a", "/api/v2/catalog/items")
        assert probe.error.startswith("ConnectionError")
        assert probe.ok is False
        assert "did not complete" in probe.verdict

    def test_a_non_json_body_does_not_break_parsing(self):
        client = FakeClient(FakeSession([("/api", FakeResponse(200, body="<html>nope</html>"))]))
        probe = run_probe(client, "n", "a", "/api/v2/catalog/items")
        assert probe.item_count is None
        assert probe.ok is True

    def test_only_the_telltale_headers_are_kept(self):
        client = FakeClient(FakeSession([("/api", FakeResponse(
            403, headers={"X-DataDome": "protected", "Set-Cookie": "secret=value",
                          "Content-Type": "text/html"}))]))
        probe = run_probe(client, "n", "a", "/api/v2/catalog/items")
        assert "x-datadome" in probe.headers
        assert "content-type" in probe.headers
        assert "set-cookie" not in probe.headers, "must not echo cookies into a public log"


class TestInterpretation:
    """One line, read on a phone, that decides what to do next."""

    def test_a_datadome_header_means_blocked_not_broken(self):
        verdict = interpret(probes_from([
            ("/api/v2/catalog", FakeResponse(404, headers={"x-datadome": "protected"})),
        ]))
        assert "Blocked, not broken" in verdict
        assert "anti-detection" in verdict

    def test_html_where_json_belongs_also_means_blocked(self):
        verdict = interpret(probes_from([
            ("/api/v2/catalog", FakeResponse(404, body="<html>challenge</html>",
                                             headers={"content-type": "text/html"})),
        ]))
        assert "Blocked, not broken" in verdict

    def test_brands_working_while_every_catalog_fails_means_the_endpoint_is_gone(self):
        """The live signature on 14 Sep: same session, one endpoint dead."""
        verdict = interpret(probes_from([
            ("/api/v2/brands", FakeResponse(200, body='{"brands":[]}')),
            ("/api/v2/catalog", FakeResponse(404, body='{"error":"not found"}',
                                             headers={"content-type": "application/json"})),
        ]))
        assert "catalog endpoint is gone" in verdict

    def test_one_working_variant_points_at_the_parameters(self):
        """The url-length hypothesis: only the long brand_ids list is refused.

        This is the case that would otherwise be misread as the endpoint being
        gone, when the fix is just to split the brand list across requests.
        """
        class RejectsLongBrandLists(FakeSession):
            def get(self, url, params=None, headers=None, timeout=None):
                params = params or {}
                if "catalog/items" in url and len(str(params.get("brand_ids", ""))) > 3:
                    return FakeResponse(404, body='{"error":"nope"}', url=url)
                return FakeResponse(200, body='{"items":[]}', url=url)

        client = FakeClient(RejectsLongBrandLists([]))
        probes = diagnose(client, catalog_id=2052, brand_ids=[1, 2, 3],
                          catalog_path="/api/v2/catalog/items", brands_path="/api/v2/brands")
        assert "parameters, not the endpoint" in interpret(probes)

    def test_everything_passing_means_it_was_transient(self):
        verdict = interpret(probes_from([]))
        assert "Everything works now" in verdict
        assert "no retry at all" in verdict

    def test_an_unreachable_homepage_is_reported_before_anything_else(self):
        verdict = interpret(probes_from([
            ("https://www.vinted.co.uk/", ConnectionError("no route")),
        ]))
        assert "unreachable" in verdict


class TestCoverage:
    def test_it_probes_the_hypotheses_we_care_about(self):
        probes = probes_from([])
        names = [p.name for p in probes]
        assert names[0] == "homepage", "reachability has to be established first"
        assert "brands" in names, "the known-good control"
        assert "catalog: bare" in names, "does the path exist at all"
        assert "catalog: live request" in names, "reproduces the real failure"
        assert "catalog: one brand" in names, "url length / param count"
        assert "catalog: no brands" in names
        assert "catalog: no category" in names

    def test_every_probe_explains_what_it_isolates(self):
        """The table is read by someone who did not write it."""
        for probe in probes_from([]):
            assert probe.asks, f"{probe.name} has no stated hypothesis"
