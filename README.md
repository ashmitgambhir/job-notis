# jobmonitor

Continuous monitoring of engineering internship, co-op and new-grad postings
across a configurable list of employers, with SMS and email alerts within
minutes of a posting appearing.

Built API-first: it talks to Greenhouse, Lever, Workday, Oracle and the rest
through their own JSON endpoints rather than scraping HTML, because those
endpoints are faster, quieter, and don't break when a careers page is
redesigned. A browser is the last resort, not the default.

```
jobmon run          # monitor continuously
jobmon scan         # one cycle, for cron or CI
jobmon discover     # auto-detect each company's careers platform
jobmon doctor       # check config, endpoints and notification channels
```

---

## Quick start

```bash
git clone <your-repo-url> job-monitor && cd job-monitor
./setup.sh                 # venv, dependencies, .env, database
$EDITOR .env               # add your phone number and email credentials
jobmon discover            # resolve careers platforms for unverified companies
jobmon doctor              # confirm everything is reachable
jobmon test-notify         # send yourself a sample alert
jobmon run                 # start monitoring
```

The first scan of each company records its whole board **silently** and marks
it seeded. You get alerts from the second scan onward — otherwise adding a
company with 2,000 open roles would send 2,000 text messages.

---

## How it works

```
                    ┌──────────────┐
  companies.yaml ──▶│  Discovery   │  fingerprint careers page → verify by
                    │              │  actually fetching postings
                    └──────┬───────┘
                           ▼
  ┌────────────────────────────────────────────┐
  │  Connectors  (API first, browser last)     │
  │  greenhouse · lever · ashby · workday ·    │
  │  oracle · icims · smartrecruiters ·        │
  │  successfactors · eightfold · phenom ·     │
  │  avature · taleo · jobvite · amazon ·      │
  │  usajobs · json-ld · html · browser        │
  └──────┬─────────────────────────────────────┘
         ▼
  ┌─────────────┐   new / changed only    ┌──────────────┐
  │  Dedupe DB  │────────────────────────▶│   Matching   │
  │  (SQLite /  │                         │ rules → semantic
  │   Postgres) │◀────── scores ──────────│ → LLM (band only)
  └─────────────┘                         └──────┬───────┘
         │ claim (unique constraint)              ▼
         └──────────────────────────────▶ ┌──────────────┐
                                          │  Dispatcher  │
                                          │ sms · email ·│
                                          │ slack ·      │
                                          │ discord ·    │
                                          │ telegram ·   │
                                          │ webhook      │
                                          └──────────────┘
```

### Speed

Detection latency is dominated by how often you poll and how much you have to
fetch each time. Three things keep it low:

- **Early-stop paging.** Workday and Oracle return newest-first. Once a page
  contains only postings already in the database, nothing newer can be further
  down, so paging stops. NVIDIA's board costs 60 requests to seed and **2
  requests per cycle** thereafter.
- **Title prefiltering.** Descriptions are fetched only for postings whose
  title could still clear the threshold. Most of a 2,000-posting board is
  eliminated for free.
- **Conditional requests.** ETag / Last-Modified revalidation means an
  unchanged board costs a 304.

Companies tagged `priority: high` poll on their own faster cadence.

### Reliability

Every company is independent. A failure is recorded, alerted on after N
consecutive occurrences, and the cycle continues — one dead careers site never
stops the rest. Retries use exponential backoff with full jitter, so companies
sharing a host (`*.myworkdayjobs.com`) don't retry in lockstep and re-trigger
the failure. Rate limiting is per-host rather than per-company for the same
reason.

### Never notifying twice

Each `(posting, channel)` pair is claimed in the database behind a unique
constraint before anything is sent. Two scanner instances racing on the same
posting cannot both win the claim. A posting that disappears and later returns
is treated as new (per spec) via an appearance counter that is part of the
dedupe key. A re-post under a fresh requisition ID with an identical
title+location is recognised as the same role and does *not* re-notify.

---

## Matching

Scoring is a weighted blend, ordered cheapest-first so most postings never
reach the expensive stages:

| Stage | Cost | What it does |
|---|---|---|
| Hard exclusions | free | Drops software, finance, marketing, nursing, senior/director roles, denied locations |
| Rule score | free | Title family, seniority, department, skills, degree |
| Semantic | local model | Embedding similarity to a role profile — catches relevant roles whose titles match no keyword |
| LLM | API call | **Borderline scores only**, capped per cycle |

The LLM is a tiebreaker, not a pipeline stage. It only sees postings scoring
inside the ambiguous band (55–88 by default), so cost scales with genuinely
uncertain postings rather than with traffic. It is off by default.

Without the `embeddings` extra installed, the semantic component is dropped
and its weight redistributed rather than substituted with a weaker
lexical score on a different scale — that would silently deduct points from
every genuine match.

**Tuning.** Everything lives in `config/roles.yaml` and `config/filters.yaml`.
Raise `matching.threshold` if you get noise; lower it if you're missing roles.
`review_threshold` records near-misses without paging you, so you can tune
from real data — `jobmon list --min-score 60` shows what sat just under the bar.

---

## Configuration

Five files, no code changes needed:

| File | Purpose |
|---|---|
| `companies.yaml` | Who to monitor, which connector, priority, score bonus |
| `roles.yaml` | Title families and weights, seniority rules, skills, degrees, semantic profile |
| `filters.yaml` | Hard exclusions, penalties, location allow/deny, employment types |
| `settings.yaml` | Intervals, concurrency, timeouts, retries, thresholds, database, logging |
| `notifications.yaml` | Channels, per-channel score floors, rate caps, quiet hours, digests |

Secrets are never in YAML — files reference `${TWILIO_AUTH_TOKEN}` and the
loader resolves them from the environment, so the config stays safe to commit.

Adding a company is three lines:

```yaml
  - name: Some Employer
    slug: someemployer
    careers_url: https://careers.example.com/
```

Leave `connector` unset and `jobmon discover` will identify the platform and
write the parameters back.

---

## Company coverage

The shipped `companies.yaml` covers all 42 requested employers. Entries carry a
`verified` flag indicating whether the endpoint was confirmed against the live
API:

**Verified against live APIs** (returned real postings during development):
Anduril, SpaceX, Rocket Lab (Greenhouse) · Shield AI (Lever) · Boeing, Abbott,
ITW, NVIDIA, Intel (Workday) · Amazon, AWS (amazon.jobs).

**Platform fingerprinted, parameters need confirming**: John Deere and Northrop
(Eightfold), Rivian and Navistar (iCIMS), Texas Instruments (Oracle), AGCO,
ExxonMobil and Hershey (SuccessFactors), RTX and United (Phenom), Siemens
(Avature), Lockheed (JSON-LD), NASA (USAJOBS).

**Auto-detect at runtime**: the remainder.

Run `jobmon discover` on first setup — it probes each unresolved company and
verifies candidates by actually fetching postings, then writes the working
parameters back into `companies.yaml`. Tesla and Apple block plain HTTP clients
and will likely need the `browser` extra.

> Careers platforms change. `verified: true` means it worked when checked, not
> that it will work forever — which is exactly why discovery, health alerts and
> `jobmon doctor` exist.

---

## Deployment

| Target | How |
|---|---|
| Local / VPS | `./setup.sh && jobmon run` |
| Docker | `docker compose up -d` |
| Railway / Render / Fly.io | Deploy the Dockerfile; set env vars; health check on `:8080/health` |
| Google Cloud Run | Use Cloud Scheduler + `jobmon scan` (Cloud Run scales to zero) |
| AWS | ECS/Fargate for the daemon, or EventBridge + Lambda running `jobmon scan` |
| GitHub Actions | `.github/workflows/monitor.yml` — zero infrastructure, but slower and less punctual |

For anything longer-lived than a laptop, point `DATABASE_URL` at Postgres so
the dedupe state survives redeploys.

`GET :8080/health` returns 200 while scans are completing on schedule and 503
when the heartbeat goes stale — a scheduler that has wedged while the process
stays up is precisely the failure this catches. `GET :8080/metrics` adds HTTP,
notification and matching counters.

---

## Commands

```bash
jobmon run                          # daemon
jobmon scan [--only SLUG] [--no-notify]
jobmon discover [--only SLUG] [--dry-run]
jobmon doctor [--no-check-endpoints]
jobmon test-notify [--score 97]
jobmon list [--days 7] [--min-score 60]
jobmon stats [--days 7]
jobmon export --format csv|json|rss [-o PATH]
jobmon health
jobmon init-db
```

---

## Development

```bash
make install    # dev + embedding extras
make test       # 125 tests, fully offline
make lint
make docker
```

Tests never touch the network — connector tests run against recorded payload
shapes with `respx`, and the integration test wires the real repository,
scorer, dispatcher and orchestrator together against a mocked board.

### Adding a connector

Write a module in `src/jobmonitor/connectors/`, subclass `Connector`, implement
`fetch_listings()` (and `fetch_detail()` if the listing endpoint is lean), then
add one line to `registry.py`. Set `ordered_by_recency = True` only if you have
verified the platform returns newest-first — that flag licenses early-stop
paging, and it would skip new postings if it were wrong.

---

## License

MIT
