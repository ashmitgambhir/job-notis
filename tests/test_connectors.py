"""Connector tests against recorded payload shapes.

The fixtures mirror the real API responses these connectors were built and
verified against, so a refactor that breaks field mapping fails here rather
than silently returning zero postings in production.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from jobmonitor.config.schema import CompanyConfig, HttpSettings
from jobmonitor.connectors.base import Connector
from jobmonitor.connectors.greenhouse import GreenhouseConnector
from jobmonitor.connectors.jsonld import find_job_postings, iter_jsonld_blocks
from jobmonitor.connectors.lever import LeverConnector
from jobmonitor.connectors.workday import WorkdayConnector
from jobmonitor.errors import PermanentError
from jobmonitor.net.client import HttpClient


@pytest.fixture
def http_settings() -> HttpSettings:
    return HttpSettings(max_retries=1, backoff_base_seconds=0.01, cache_ttl_seconds=0,
                        requests_per_second_per_host=1000, http2=False)


@pytest.fixture
async def client(http_settings):
    async with HttpClient(http_settings) as c:
        yield c


class TestGreenhouse:
    @pytest.fixture
    def company(self):
        return CompanyConfig(
            name="Anduril", slug="anduril", connector="greenhouse",
            params={"board_token": "andurilindustries"},
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_parses_listings(self, client, company):
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 4567,
                            "title": "2026 Early Career Mechanical Engineer",
                            "updated_at": "2026-07-20T14:00:00-04:00",
                            "first_published": "2026-07-19T09:00:00-04:00",
                            "absolute_url": "https://boards.greenhouse.io/x/jobs/4567",
                            "location": {"name": "Costa Mesa, California"},
                            "departments": [{"name": "Hardware Engineering"}],
                            "requisition_id": "R-1234",
                        }
                    ]
                },
            )
        )
        postings = await GreenhouseConnector(company, client).fetch_listings()
        assert len(postings) == 1
        p = postings[0]
        assert p.external_id == "4567"
        assert p.title == "2026 Early Career Mechanical Engineer"
        assert p.location == "Costa Mesa, California"
        assert p.department == "Hardware Engineering"
        assert p.source_platform == "greenhouse"
        # Listings are fetched lean; the description comes later.
        assert p.detail_pending

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_detail_strips_html(self, client, company):
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs/4567"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"content": "<p>Design <b>mechanical</b> parts.</p><ul><li>CAD</li></ul>"},
            )
        )
        posting = GreenhouseConnector(company, client)._posting(
            external_id="4567", title="X", url="u", detail_pending=True
        )
        enriched = await GreenhouseConnector(company, client).fetch_detail(posting)
        assert "Design mechanical parts." in enriched.description
        assert "<b>" not in enriched.description
        assert not enriched.detail_pending

    @respx.mock
    @pytest.mark.asyncio
    async def test_404_is_permanent(self, client, company):
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs"
        ).mock(return_value=httpx.Response(404))
        with pytest.raises(PermanentError):
            await GreenhouseConnector(company, client).fetch_listings()


class TestLever:
    @respx.mock
    @pytest.mark.asyncio
    async def test_parses_inline_description(self, client):
        company = CompanyConfig(
            name="Shield AI", slug="shieldai", connector="lever",
            params={"board_token": "shieldai"},
        )
        respx.get("https://api.lever.co/v0/postings/shieldai").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "abc-123",
                        "text": "Mechanical Engineer I",
                        "hostedUrl": "https://jobs.lever.co/shieldai/abc-123",
                        "createdAt": 1753000000000,
                        "categories": {
                            "location": "San Diego, CA",
                            "department": "Hardware",
                            "commitment": "Full-time",
                        },
                        "descriptionPlain": "Design mechanical subsystems.",
                        "lists": [
                            {"text": "Requirements",
                             "content": "<li>BS in Mechanical Engineering</li>"}
                        ],
                    }
                ],
            )
        )
        postings = await LeverConnector(company, client).fetch_listings()
        assert len(postings) == 1
        p = postings[0]
        assert p.external_id == "abc-123"
        assert p.location == "San Diego, CA"
        # Lever returns everything inline, so no detail round-trip is needed.
        assert not p.detail_pending
        assert "BS in Mechanical Engineering" in p.description
        assert p.posted_at is not None and p.posted_at.year == 2025


class TestWorkday:
    @pytest.fixture
    def company(self):
        return CompanyConfig(
            name="Boeing", slug="boeing", connector="workday",
            params={"tenant": "boeing", "pod": "wd1", "site": "EXTERNAL_CAREERS"},
        )

    @staticmethod
    def _page(offset: int, total: int, count: int = 20):
        return {
            "total": total if offset == 0 else 0,
            "jobPostings": [
                {
                    "title": f"Engineer {offset + i}",
                    "externalPath": f"/job/USA/Engineer_JR{offset + i}",
                    "locationsText": "USA - Everett WA",
                    "postedOn": "Posted Today",
                    "bulletFields": [f"JR{offset + i}"],
                }
                for i in range(count)
            ],
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_pagination_uses_total_from_first_page_only(self, client, company):
        """Workday reports `total` only on page 1; later pages say 0.

        Trusting the per-page value stopped paging after 40 postings.
        """
        url = "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/jobs"
        calls = {"n": 0}

        def responder(request):
            offset = json.loads(request.content)["offset"]
            calls["n"] += 1
            remaining = max(0, 50 - offset)
            return httpx.Response(200, json=self._page(offset, 50, min(20, remaining)))

        respx.post(url).mock(side_effect=responder)
        postings = await WorkdayConnector(company, client).fetch_listings()
        assert len(postings) == 50
        assert calls["n"] == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_early_stop_when_all_known(self, client, company):
        """Recency-ordered boards can stop once a page holds nothing new."""
        url = "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/jobs"
        calls = {"n": 0}

        def responder(request):
            offset = json.loads(request.content)["offset"]
            calls["n"] += 1
            return httpx.Response(200, json=self._page(offset, 400))

        respx.post(url).mock(side_effect=responder)
        connector = WorkdayConnector(company, client)
        connector.known_ids = {f"JR{i}" for i in range(400)}
        await connector.fetch_listings()
        # Page 1 always runs; page 2 is all-known, so paging stops there.
        assert calls["n"] == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_early_stop_on_first_ever_scan(self, client, company):
        url = "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/jobs"

        def responder(request):
            offset = json.loads(request.content)["offset"]
            remaining = max(0, 60 - offset)
            return httpx.Response(200, json=self._page(offset, 60, min(20, remaining)))

        respx.post(url).mock(side_effect=responder)
        connector = WorkdayConnector(company, client)
        connector.known_ids = set()
        assert len(await connector.fetch_listings()) == 60

    def test_requisition_id_preferred_over_url_path(self, company, http_settings):
        job = {"bulletFields": ["JR2015626"]}
        assert WorkdayConnector._external_id(job, "/job/X/Y_JR999") == "JR2015626"
        assert WorkdayConnector._external_id({}, "/job/X/Y_JR999") == "Y_JR999"


class TestRelativeDates:
    @pytest.mark.parametrize(
        "text,max_days",
        [("Posted Today", 0.1), ("Posted Yesterday", 1.1), ("Posted 5 Days Ago", 5.1)],
    )
    def test_workday_style_strings(self, text, max_days):
        from jobmonitor.core.models import utcnow

        parsed = Connector.parse_date(text)
        assert parsed is not None
        assert (utcnow() - parsed).total_seconds() / 86400 <= max_days

    def test_epoch_milliseconds(self):
        parsed = Connector.parse_date(1753000000000)
        assert parsed is not None and parsed.year == 2025

    def test_iso_string(self):
        parsed = Connector.parse_date("2026-07-20T14:00:00-04:00")
        assert parsed is not None and parsed.month == 7

    def test_garbage_returns_none(self):
        assert Connector.parse_date("not a date at all") is None
        assert Connector.parse_date(None) is None


class TestJsonLd:
    HTML = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting",
     "title":"Mechanical Engineer","datePosted":"2026-07-01",
     "description":"<p>Design parts.</p>",
     "identifier":{"@type":"PropertyValue","value":"REQ-9"},
     "jobLocation":{"@type":"Place","address":{"addressLocality":"Peoria",
       "addressRegion":"IL","addressCountry":"US"}}}
    </script></head><body></body></html>
    """

    def test_extracts_posting(self):
        blocks = list(iter_jsonld_blocks(self.HTML))
        nodes = [n for b in blocks for n in find_job_postings(b)]
        assert len(nodes) == 1
        assert nodes[0]["title"] == "Mechanical Engineer"

    def test_walks_nested_graph(self):
        html = """<script type="application/ld+json">
        {"@graph":[{"@type":"WebSite"},{"@type":"JobPosting","title":"Design Engineer"}]}
        </script>"""
        nodes = [n for b in iter_jsonld_blocks(html) for n in find_job_postings(b)]
        assert [n["title"] for n in nodes] == ["Design Engineer"]

    def test_builds_posting_with_location(self):
        from jobmonitor.connectors.jsonld import posting_from_jsonld

        node = next(n for b in iter_jsonld_blocks(self.HTML) for n in find_job_postings(b))
        posting = posting_from_jsonld(
            node, company="Caterpillar", slug="caterpillar",
            platform="jsonld", fallback_url="https://x",
        )
        assert posting is not None
        assert posting.external_id == "REQ-9"
        assert posting.location == "Peoria, IL, US"
        assert "Design parts." in posting.description


class TestHtmlStripping:
    def test_removes_scripts_and_tags(self):
        html = "<div><script>bad()</script><p>Hello&nbsp;world</p></div>"
        assert Connector.strip_html(html) == "Hello world"

    def test_handles_none(self):
        assert Connector.strip_html(None) == ""


class TestJoinLocations:
    def test_flattens_shapes(self):
        assert Connector.join_locations("Peoria, IL") == "Peoria, IL"
        assert Connector.join_locations({"name": "Austin"}) == "Austin"
        assert Connector.join_locations([{"name": "A"}, {"name": "B"}]) == "A, B"
        assert Connector.join_locations(None) is None
