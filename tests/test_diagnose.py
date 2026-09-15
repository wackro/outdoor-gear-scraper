"""Diagnosing why the API is refusing us.

The probe exists because `_request` throws the response body away, so when the
catalog endpoint started returning 404 there was no way to tell a moved endpoint
from a bot wall from a rejected parameter -- three problems with three different
fixes and one identical symptom. What is tested here is that it tells them apart.

Offline throughout: the session is a stub, because the suite has no network and
a diagnostic that only works against the live site is not testable at all.
"""
import pytest

from src.vinted.diagnose import (
    Probe, diagnose, discover_api_paths, hunt_for_replacement, interpret,
    run_probe, summarise_hunt,
)


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
        assert probe.verdict.startswith("OK, 2 items")

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


class TestAcceptanceBar:
    """A 200 is not success. `favourite_count` is.

    It is the entire hotness signal, so an endpoint that serves listings without
    it would restore the website and leave the alerts permanently dead — a dead
    end wearing the costume of a solution.
    """

    def _probe(self, body):
        client = FakeClient(FakeSession([("/api", FakeResponse(200, body=body))]))
        return run_probe(client, "n", "a", "/api/v2/items")

    def test_items_with_the_counter_are_usable(self):
        probe = self._probe('{"items":[{"id":1,"favourite_count":4}]}')
        assert probe.usable is True
        assert "with favourite_count" in probe.verdict

    def test_items_without_the_counter_are_not(self):
        probe = self._probe('{"items":[{"id":1,"title":"Jacket"}]}')
        assert probe.ok is True, "it did answer"
        assert probe.usable is False, "but it is no use to us"
        assert "**no** favourite_count" in probe.verdict

    def test_a_null_counter_counts_as_absent(self):
        """Vinted omits it on some listings; null is not a value."""
        assert self._probe('{"items":[{"id":1,"favourite_count":null}]}').usable is False

    def test_an_empty_items_array_is_not_a_find(self):
        probe = self._probe('{"items":[]}')
        assert probe.usable is False
        assert "zero items" in probe.verdict

    def test_a_200_carrying_no_items_at_all_is_not_a_find(self):
        assert self._probe('{"brands":[{"id":1}]}').usable is False


class TestDiscovery:
    def test_it_reads_paths_out_of_escaped_server_rendered_json(self):
        """The page embeds JSON with escaped slashes, exactly as the category
        tree does — which is why `fetch_catalog_tree` unescapes before matching."""
        page = r'<html><script>{"endpoint":"\/api\/v2\/catalog\/items","u":"\/api\/v2\/users"}</script></html>'
        client = FakeClient(FakeSession([("/catalog", FakeResponse(200, body=page))]))
        paths, notes = discover_api_paths(client)
        assert "/api/v2/catalog/items" in paths
        assert "/api/v2/users" in paths
        assert any("named in the page" in n for n in notes)

    def test_listing_paths_are_ranked_first(self):
        """Whoever reads this on a phone should see the plausible ones first."""
        page = '<html>"/api/v2/users" "/api/v2/zzz" "/api/v2/catalog/items"</html>'
        client = FakeClient(FakeSession([("/catalog", FakeResponse(200, body=page))]))
        paths, _ = discover_api_paths(client)
        assert paths[0] == "/api/v2/catalog/items"

    def test_it_reads_the_bundles_where_the_fetch_calls_live(self):
        page = '<html><script src="/assets/app.js"></script></html>'
        client = FakeClient(FakeSession([
            ("/catalog", FakeResponse(200, body=page)),
            ("app.js", FakeResponse(200, body='fetch("/api/v2/item_feed")')),
        ]))
        paths, notes = discover_api_paths(client)
        assert "/api/v2/item_feed" in paths
        assert any("app.js" in n for n in notes)

    def test_it_does_not_download_the_whole_application(self):
        """Bundles run to megabytes and there are dozens of them."""
        from src.vinted.diagnose import MAX_BUNDLES
        page = "<html>" + "".join(
            f'<script src="/a{i}.js"></script>' for i in range(20)) + "</html>"
        session = FakeSession([("/catalog", FakeResponse(200, body=page))])
        discover_api_paths(FakeClient(session))
        bundle_reads = [c for c in session.calls if ".js" in c[0]]
        assert len(bundle_reads) <= MAX_BUNDLES

    def test_an_unreachable_page_is_reported_not_raised(self):
        client = FakeClient(FakeSession([("/catalog", ConnectionError("refused"))]))
        paths, notes = discover_api_paths(client)
        assert paths == []
        assert any("could not fetch" in n for n in notes)

    def test_a_failing_bundle_does_not_abort_the_hunt(self):
        page = ('<html>"/api/v2/from_page"<script src="/bad.js"></script></html>')
        client = FakeClient(FakeSession([
            ("/catalog", FakeResponse(200, body=page)),
            ("bad.js", ConnectionError("gone")),
        ]))
        paths, notes = discover_api_paths(client)
        assert "/api/v2/from_page" in paths, "the page's own paths still count"
        assert any("failed" in n for n in notes)


class TestHuntVerdict:
    def _hunt(self, rules):
        client = FakeClient(FakeSession(rules))
        return hunt_for_replacement(client, catalog_id=2052, brand_ids=[1])[0]

    def test_a_working_replacement_is_named(self):
        probes = self._hunt([
            ("/catalog", FakeResponse(200, body="<html></html>")),
            ("/api/v2/items", FakeResponse(200, body='{"items":[{"favourite_count":3}]}')),
            ("/api/v2/", FakeResponse(404, body='{"code":104}')),
        ])
        verdict = summarise_hunt(probes)
        assert "Found a replacement" in verdict
        assert "/api/v2/items" in verdict

    def test_listings_without_the_counter_get_their_own_verdict(self):
        """Not a success and not a failure — a decision to be made."""
        probes = self._hunt([
            ("/catalog", FakeResponse(200, body="<html></html>")),
            ("/api/v2/items", FakeResponse(200, body='{"items":[{"id":1}]}')),
            ("/api/v2/", FakeResponse(404, body='{"code":104}')),
        ])
        verdict = summarise_hunt(probes)
        assert "not enough" in verdict
        assert "alerts permanently dead" in verdict

    def test_nothing_found_says_so_honestly(self):
        probes = self._hunt([
            ("/catalog", FakeResponse(200, body="<html></html>")),
            ("/api/", FakeResponse(404, body='{"code":104,"message":"Content not found"}')),
        ])
        verdict = summarise_hunt(probes)
        assert "No replacement found" in verdict
        assert "catalog HTML" in verdict, "it should name the remaining route"

    def test_the_dead_endpoint_is_probed_as_a_control(self):
        probes = self._hunt([("/catalog", FakeResponse(200, body="<html></html>"))])
        assert "/api/v2/catalog/items" in [p.name for p in probes]


class TestCatalogueHost:
    """The catalogue moved to its own host; the rest of the API did not.

    That split is the whole explanation for the outage: /api/v2/brands kept
    answering from www while every catalogue request 404'd, which looked like
    one endpoint being retired and was actually a relocation.
    """

    def test_www_becomes_api(self):
        from src.vinted.diagnose import catalogue_host
        assert catalogue_host("https://www.vinted.co.uk") == "https://api.vinted.co.uk"

    def test_a_host_without_www_is_left_alone(self):
        from src.vinted.diagnose import catalogue_host
        assert catalogue_host("https://api.vinted.co.uk") == "https://api.vinted.co.uk"

    def test_only_the_host_is_rewritten(self):
        """A path segment that happens to contain 'www.' must survive."""
        from src.vinted.diagnose import catalogue_host
        assert catalogue_host("https://www.vinted.co.uk/x/www.y") \
            == "https://api.vinted.co.uk/x/www.y"


class TestWarmup:
    def test_it_reads_both_credentials_from_the_homepage(self):
        """Neither costs an extra request: the client already fetches this page
        for its cookies."""
        from src.vinted.diagnose import warmup
        page = 'window.CSRF_TOKEN = "3f2504e0-4f89-11d3-9a0c-0305e82c3301";'
        client = FakeClient(FakeSession([
            ("vinted.co.uk/", FakeResponse(200, body=page, headers={"X-Anon-Id": "abc-123"})),
        ]))
        headers, notes = warmup(client)
        assert headers == {"X-Anon-Id": "abc-123",
                           "X-Csrf-Token": "3f2504e0-4f89-11d3-9a0c-0305e82c3301"}

    def test_a_missing_credential_is_reported_not_fatal(self):
        """We still want to see what the service says without it."""
        from src.vinted.diagnose import warmup
        client = FakeClient(FakeSession([("vinted.co.uk/", FakeResponse(200, body="<html/>"))]))
        headers, notes = warmup(client)
        assert headers == {}
        assert any("not offered" in n for n in notes)
        assert any("not found" in n for n in notes)

    def test_a_failed_warmup_is_a_note_not_a_crash(self):
        from src.vinted.diagnose import warmup
        client = FakeClient(FakeSession([("vinted.co.uk/", ConnectionError("down"))]))
        headers, notes = warmup(client)
        assert headers == {}
        assert any("warmup request failed" in n for n in notes)


class TestFieldCoverage:
    """Presence on one listing is not enough to build on.

    Vinted legitimately omits some of these per listing, so what matters is how
    often we can rely on a field — a field on 5% of results looks like success
    in a sample and starves the signal in production.
    """

    def _probe(self, body):
        client = FakeClient(FakeSession([("/api", FakeResponse(200, body=body))]))
        return run_probe(client, "n", "a", "/api/v2/x")

    def test_it_reports_a_percentage_per_field(self):
        probe = self._probe(
            '{"items":[{"favourite_count":1,"brand_title":"Rab"},'
            '          {"favourite_count":2},'
            '          {"brand_title":"Rab"},'
            '          {}]}')
        assert probe.coverage["favourite_count"] == 50.0
        assert probe.coverage["brand_title"] == 50.0
        assert probe.coverage["status"] == 0.0

    def test_partial_coverage_still_counts_as_present(self):
        """One listing in twenty is enough to prove the field exists at all —
        the percentage is what says whether it is dependable."""
        items = ",".join(["{}"] * 19 + ['{"favourite_count":3}'])
        probe = self._probe(f'{{"items":[{items}]}}')
        assert probe.has_favourite_count is True
        assert probe.coverage["favourite_count"] == 5.0

    def test_no_items_means_no_coverage_claims(self):
        assert self._probe('{"items":[]}').coverage == {}


class TestServiceCandidate:
    def test_the_relocated_endpoint_is_probed_first(self):
        """It is not a guess — it is somebody else's working code."""
        client = FakeClient(FakeSession([]))
        probes, _ = hunt_for_replacement(client, catalog_id=2052, brand_ids=[1])
        assert probes[0].name == "svc-catalogue/items"
        assert "api.vinted.co.uk/svc-catalogue/items" in probes[0].url

    def test_it_sends_the_restructured_filters(self):
        session = FakeSession([])
        hunt_for_replacement(FakeClient(session), catalog_id=2052, brand_ids=[99])
        catalogue = [c for c in session.calls if "svc-catalogue" in c[0]]
        assert catalogue, "it never called the service"
        params = catalogue[0][1]
        assert params["attribute_ids[catalog]"] == 2052
        assert params["attribute_ids[brand]"] == "99"
        assert "catalog_ids" not in params, "the old filter shape is gone"

    def test_it_also_asks_whether_the_auth_headers_are_required(self):
        client = FakeClient(FakeSession([]))
        probes, _ = hunt_for_replacement(client, catalog_id=2052, brand_ids=[1])
        assert any("no auth headers" in p.name for p in probes)
