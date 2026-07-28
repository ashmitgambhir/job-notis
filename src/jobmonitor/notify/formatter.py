"""Notification rendering.

One :class:`Alert` per matched posting, rendered per channel. SMS gets a
stripped-down version because it is the channel that wakes you up and every
character costs a segment; email carries the full detail.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..core.models import JobPosting, MatchResult, utcnow

WORK_MODE_LABEL = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "onsite": "Onsite",
    "unknown": "Not stated",
}

EMPLOYMENT_LABEL = {
    "intern": "Intern",
    "co-op": "Co-op",
    "new_grad": "New Grad",
    "full_time": "Full Time",
    "part_time": "Part Time",
    "contract": "Contract",
    "unknown": "Not stated",
}


def humanize_age(posted_at: Optional[datetime], now: Optional[datetime] = None) -> str:
    """"3 minutes ago" — the number that tells you whether you are first."""
    if posted_at is None:
        return "unknown"
    now = now or utcnow()
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    seconds = (now - posted_at).total_seconds()
    if seconds < 0:
        return "just now"
    minutes = seconds / 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        n = int(minutes)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    hours = minutes / 60
    if hours < 24:
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    days = int(hours / 24)
    return f"{days} day{'s' if days != 1 else ''} ago"


@dataclass
class Alert:
    posting: JobPosting
    match: MatchResult
    detected_at: datetime
    timezone_name: str = "America/Chicago"

    # ------------------------------------------------------------------ #

    @property
    def local_detected(self) -> str:
        try:
            from zoneinfo import ZoneInfo

            local = self.detected_at.astimezone(ZoneInfo(self.timezone_name))
            return local.strftime("%I:%M %p %Z").lstrip("0")
        except Exception:
            # A bad tz name in config should not stop a notification going out.
            return self.detected_at.strftime("%I:%M %p UTC").lstrip("0")

    @property
    def employment(self) -> str:
        return EMPLOYMENT_LABEL.get(self.posting.employment_type.value, "Not stated")

    @property
    def work_mode(self) -> str:
        return WORK_MODE_LABEL.get(self.posting.work_mode.value, "Not stated")

    @property
    def posted_age(self) -> str:
        return humanize_age(self.posting.posted_at, self.detected_at)

    def fields(self) -> List[tuple]:
        """Every field the spec asks a notification to carry."""
        rows = [
            ("Company", self.posting.company),
            ("Title", self.posting.title),
        ]
        if self.posting.department:
            rows.append(("Department", self.posting.department))
        rows.extend(
            [
                ("Location", self.posting.location or "Not stated"),
                ("Work Mode", self.work_mode),
                ("Type", self.employment),
                ("Posted", self.posted_age),
                ("Detected", self.local_detected),
                ("Match Score", f"{self.match.score}%"),
            ]
        )
        if self.posting.deadline:
            rows.append(("Apply By", self.posting.deadline.strftime("%b %d, %Y")))
        if self.posting.requires_clearance:
            rows.append(("Clearance", "Required"))
        if self.posting.offers_sponsorship is False:
            rows.append(("Sponsorship", "Not offered"))
        return rows

    # ------------------------------------------------------------------ #

    def sms_text(self) -> str:
        """Short by design — a text message is a nudge, the link has the detail."""
        lines = [
            "NEW JOB",
            f"{self.posting.company}: {self.posting.title}",
            f"{self.posting.location or 'Location n/a'} | {self.employment}",
            f"Posted {self.posted_age} | Score {self.match.score}%",
            self.posting.url,
        ]
        return "\n".join(lines)

    def plain_text(self) -> str:
        lines = ["NEW JOB FOUND", ""]
        for label, value in self.fields():
            lines.append(f"{label}: {value}")
        if self.match.top_reasons:
            lines.append("")
            lines.append("Why it matched:")
            lines.extend(f"  - {r}" for r in self.match.top_reasons)
        lines.extend(["", "Apply:", self.posting.url])
        return "\n".join(lines)

    def markdown(self) -> str:
        """Used by Slack, Discord and Telegram."""
        lines = [f"*NEW JOB FOUND — {self.match.score}% match*", ""]
        for label, value in self.fields():
            if label == "Match Score":
                continue
            lines.append(f"*{label}:* {value}")
        if self.match.top_reasons:
            lines.append("")
            lines.append("*Why it matched:*")
            lines.extend(f"• {r}" for r in self.match.top_reasons)
        lines.extend(["", f"<{self.posting.url}|Apply now>"])
        return "\n".join(lines)

    def telegram_text(self) -> str:
        lines = [f"<b>NEW JOB FOUND — {self.match.score}% match</b>", ""]
        for label, value in self.fields():
            if label == "Match Score":
                continue
            lines.append(f"<b>{html.escape(label)}:</b> {html.escape(str(value))}")
        if self.match.top_reasons:
            lines.append("")
            lines.append("<b>Why it matched:</b>")
            lines.extend(f"• {html.escape(r)}" for r in self.match.top_reasons)
        lines.extend(["", f'<a href="{html.escape(self.posting.url)}">Apply now</a>'])
        return "\n".join(lines)

    def email_subject(self) -> str:
        return (
            f"[{self.match.score}%] {self.posting.company} — {self.posting.title}"
        )

    def email_html(self) -> str:
        rows = "".join(
            f'<tr><td style="padding:6px 14px 6px 0;color:#666;'
            f'white-space:nowrap;vertical-align:top">{html.escape(label)}</td>'
            f'<td style="padding:6px 0;color:#111"><strong>{html.escape(str(value))}'
            f"</strong></td></tr>"
            for label, value in self.fields()
        )
        reasons = ""
        if self.match.top_reasons:
            items = "".join(
                f'<li style="margin:3px 0">{html.escape(r)}</li>'
                for r in self.match.top_reasons
            )
            reasons = (
                '<p style="margin:20px 0 6px;color:#666;font-size:13px">'
                "WHY IT MATCHED</p>"
                f'<ul style="margin:0;padding-left:20px;color:#111">{items}</ul>'
            )
        score_color = "#0a7d33" if self.match.score >= 85 else "#b06a00"
        return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
            max-width:560px;margin:0 auto;padding:24px;color:#111">
  <div style="border-left:4px solid {score_color};padding-left:14px;margin-bottom:22px">
    <div style="font-size:12px;letter-spacing:.08em;color:#666">NEW JOB FOUND</div>
    <div style="font-size:21px;font-weight:600;margin-top:4px">
      {html.escape(self.posting.title)}
    </div>
    <div style="font-size:15px;color:#444;margin-top:2px">
      {html.escape(self.posting.company)}
    </div>
  </div>
  <table style="border-collapse:collapse;font-size:14px;width:100%">{rows}</table>
  {reasons}
  <p style="margin:26px 0 0">
    <a href="{html.escape(self.posting.url)}"
       style="background:#111;color:#fff;padding:11px 22px;border-radius:6px;
              text-decoration:none;font-weight:600;display:inline-block">Apply now</a>
  </p>
  <p style="margin-top:26px;font-size:11px;color:#999">
    Sent by jobmonitor. Adjust the score threshold in config/settings.yaml.
  </p>
</div>"""

    def webhook_payload(self) -> Dict[str, Any]:
        return {
            "event": "job.matched",
            "company": self.posting.company,
            "company_slug": self.posting.company_slug,
            "title": self.posting.title,
            "department": self.posting.department,
            "location": self.posting.location,
            "work_mode": self.posting.work_mode.value,
            "employment_type": self.posting.employment_type.value,
            "url": self.posting.url,
            "score": self.match.score,
            "reasons": self.match.top_reasons,
            "posted_at": self.posting.posted_at.isoformat() if self.posting.posted_at else None,
            "detected_at": self.detected_at.isoformat(),
            "source_platform": self.posting.source_platform,
            "requires_clearance": self.posting.requires_clearance,
            "offers_sponsorship": self.posting.offers_sponsorship,
            "deadline": self.posting.deadline.isoformat() if self.posting.deadline else None,
        }


def render_digest(matches: List[Dict[str, Any]], title: str, since_label: str) -> str:
    """HTML body for the daily/weekly summary."""
    if not matches:
        body = '<p style="color:#666">No new matches in this period.</p>'
    else:
        rows = "".join(
            f"""<tr style="border-bottom:1px solid #eee">
  <td style="padding:9px 12px 9px 0;font-weight:600;white-space:nowrap">{m['score']}%</td>
  <td style="padding:9px 12px 9px 0">
    <a href="{html.escape(m['url'])}" style="color:#111;text-decoration:none">
      <strong>{html.escape(m['title'])}</strong></a><br>
    <span style="color:#666;font-size:13px">
      {html.escape(m['company'])} &middot; {html.escape(m.get('location') or 'n/a')}
    </span>
  </td>
</tr>"""
            for m in matches
        )
        body = f'<table style="border-collapse:collapse;width:100%;font-size:14px">{rows}</table>'
    return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
            max-width:620px;margin:0 auto;padding:24px;color:#111">
  <h2 style="margin:0 0 4px">{html.escape(title)}</h2>
  <p style="margin:0 0 20px;color:#666;font-size:13px">{html.escape(since_label)}</p>
  {body}
</div>"""
