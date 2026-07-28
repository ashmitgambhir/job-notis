"""CSV / JSON / RSS export of tracked postings."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from xml.sax.saxutils import escape

COLUMNS = [
    "score",
    "matched",
    "company",
    "title",
    "department",
    "location",
    "employment_type",
    "work_mode",
    "posted_at",
    "first_seen_at",
    "source_platform",
    "closed",
    "url",
]


def to_csv(rows: List[Dict[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in COLUMNS})
    return len(rows)


def to_json(rows: List[Dict[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(rows),
                "jobs": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return len(rows)


def to_rss(rows: List[Dict[str, Any]], path: Path, title: str = "jobmonitor matches") -> int:
    """An RSS feed makes the matches readable in any reader without an API."""
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = []
    for row in rows[:200]:
        description = (
            f"{row.get('company')} — {row.get('location') or 'location not stated'} — "
            f"match {row.get('score')}%"
        )
        reasons = row.get("reasons") or []
        if reasons:
            description += " — " + "; ".join(reasons)
        items.append(
            "    <item>\n"
            f"      <title>{escape(str(row.get('title') or ''))}</title>\n"
            f"      <link>{escape(str(row.get('url') or ''))}</link>\n"
            f"      <guid isPermaLink=\"false\">"
            f"{escape(str(row.get('company_slug')))}:{escape(str(row.get('url')))}</guid>\n"
            f"      <description>{escape(description)}</description>\n"
            "    </item>"
        )
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        "    <link>https://localhost/</link>\n"
        "    <description>Early-career engineering roles matched by jobmonitor"
        "</description>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        + "\n".join(items)
        + "\n  </channel>\n</rss>\n"
    )
    path.write_text(feed, encoding="utf-8")
    return min(len(rows), 200)
