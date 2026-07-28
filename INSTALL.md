# Installation guide

From clone to running monitor. Roughly 15 minutes, most of it spent creating
accounts for whichever notification channels you want.

---

## 1. Requirements

- **Python 3.9 or newer** (`python3 --version`)
- ~2 GB disk if you want local semantic matching
- Docker, only if deploying that way

---

## 2. Install

```bash
git clone <your-repo-url> job-monitor
cd job-monitor
./setup.sh
```

`setup.sh` creates a virtualenv, installs the package, copies `.env.example`
to `.env`, and initialises the database. It will ask whether to install local
semantic matching — say yes unless disk space is tight. It substantially
improves matching, runs locally, and costs nothing per posting.

To skip the prompt: `WITH_EMBEDDINGS=1 ./setup.sh` (or `WITH_EMBEDDINGS=0`).

Activate the environment in new shells with `source .venv/bin/activate`.

<details>
<summary>Manual install</summary>

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -e ".[embeddings]"
cp .env.example .env
jobmon init-db
```
</details>

---

## 3. Set up notifications

Open `.env` and fill in the channels you want. **You supply your own
credentials — nothing is pre-filled, and `.env` is gitignored so it is never
committed.**

### SMS via Twilio

This is the channel that actually wakes you up. Twilio's trial credit covers
plenty of alerts.

1. Sign up at <https://www.twilio.com/try-twilio>.
2. From the Console dashboard, copy the **Account SID** and **Auth Token**.
3. Get a phone number: **Phone Numbers → Manage → Buy a number** (pick one with
   SMS capability).
4. On a trial account, verify your own number under **Phone Numbers → Verified
   Caller IDs** — trials can only text verified numbers.
5. Fill in `.env`:

```ini
SMS_ENABLED=true
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_FROM_NUMBER=+15551234567     # the number you bought
ALERT_PHONE_NUMBER=+15559876543     # YOUR phone, E.164 format
```

`ALERT_PHONE_NUMBER` is your own mobile number in E.164 format: `+`, country
code, then the number with no spaces or dashes. US example: `+13125551234`.

### Email via Gmail

Gmail needs an **App Password**, not your account password.

1. Enable 2-Step Verification: <https://myaccount.google.com/security>.
2. Create an App Password: <https://myaccount.google.com/apppasswords> — pick
   "Mail", then copy the 16-character password.
3. Fill in `.env`:

```ini
EMAIL_ENABLED=true
EMAIL_PROVIDER=smtp
SMTP_HOST=smtp.gmail.com
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=abcdefghijklmnop     # the 16-char app password
ALERT_FROM_EMAIL=you@gmail.com
ALERT_EMAIL=you@gmail.com
```

Other providers: Outlook `smtp-mail.outlook.com`, Fastmail `smtp.fastmail.com`,
port 587 for all. For SendGrid, set `EMAIL_PROVIDER=sendgrid` and
`SENDGRID_API_KEY`.

### Slack / Discord / Telegram (optional)

- **Slack**: create an Incoming Webhook at
  <https://api.slack.com/messaging/webhooks>, set `SLACK_WEBHOOK_URL`.
- **Discord**: Server Settings → Integrations → Webhooks → New Webhook, copy
  the URL into `DISCORD_WEBHOOK_URL`.
- **Telegram**: message `@BotFather` to create a bot and get a token; message
  `@userinfobot` to get your chat ID.

Set the matching `*_ENABLED=true` for each.

### Per-channel thresholds

`config/notifications.yaml` sets a score floor per channel. The defaults send
SMS only for strong matches (85+) and email for everything worth a look (75+),
so your phone stays quiet while your inbox stays complete. Adjust to taste.

---

## 4. Resolve the company list

```bash
jobmon discover
```

This probes each unverified company: it fetches the careers page, looks for
platform fingerprints, tries board-token guesses, and **verifies each candidate
by actually fetching postings**. Working parameters are written back into
`config/companies.yaml`.

Takes a few minutes and makes real requests. Some companies will fail — Tesla
and Apple block plain HTTP clients. Options for those:

- Install the browser fallback: `pip install ".[browser]" && playwright install chromium`,
  then set `connector: browser` with `params: {url: ...}` for that company.
- Or set `enabled: false` and move on.

---

## 5. Verify

```bash
jobmon doctor
```

Checks configuration, reports the semantic backend, validates every enabled
notification channel, and calls each company's endpoint. Fix anything red.

Then send yourself a test alert:

```bash
jobmon test-notify
```

You should get a text and/or email within seconds. If not, `jobmon doctor` will
have already told you which channel is misconfigured.

---

## 6. First run

```bash
jobmon scan --no-notify
```

One cycle, no alerts. This seeds the database with every current posting so you
aren't paged about the existing backlog. Check what it found:

```bash
jobmon list --days 1 --min-score 60
jobmon stats
```

If the scores look wrong, tune `config/roles.yaml` and `config/filters.yaml`
and re-run — nothing is notified until you're happy.

Then start monitoring:

```bash
jobmon run
```

---

## 7. Keep it running

### systemd (Linux VPS)

```ini
# /etc/systemd/system/jobmonitor.service
[Unit]
Description=jobmonitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/job-monitor
ExecStart=/home/youruser/job-monitor/.venv/bin/jobmon run
Restart=always
RestartSec=15
Environment=LOG_FORMAT=json

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now jobmonitor
journalctl -u jobmonitor -f
```

### launchd (macOS)

```bash
cat > ~/Library/LaunchAgents/com.jobmonitor.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jobmonitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/job-monitor/.venv/bin/jobmon</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/YOU/job-monitor</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/jobmonitor.err</string>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.jobmonitor.plist
```

Note macOS sleep suspends the process. For genuinely continuous monitoring, use
a cloud host.

### Docker

```bash
docker compose up -d
docker compose logs -f
curl localhost:8080/health
```

### Railway / Render / Fly.io

Deploy the Dockerfile, add the `.env` values as environment variables, and
point the platform's health check at `/health` on port 8080. Attach a
persistent volume at `/app/data`, or set `DATABASE_URL` to a managed Postgres
instance — without persistence the dedupe state resets on every deploy and you
get re-notified about everything.

### GitHub Actions

Zero infrastructure. Add your credentials as repository secrets (Settings →
Secrets and variables → Actions) matching the names in
`.github/workflows/monitor.yml`, then enable the workflow. Detection is slower
and less punctual than the daemon, since scheduled runs queue under load.

---

## Troubleshooting

**No notifications at all.** Run `jobmon doctor`. Most often a channel is
enabled but missing credentials, or the first scan is still seeding — seeding
never notifies by design.

**Too many / too few matches.** Adjust `matching.threshold` in
`config/settings.yaml`. Use `jobmon list --min-score 60` to see near-misses and
decide which direction to move.

**A company keeps failing.** `jobmon discover --only <slug>` to re-detect its
platform. Careers sites migrate between vendors; this is expected maintenance,
and the health alert exists to tell you when it happens.

**`semantic backend: lexical`.** The embeddings extra isn't installed:
`pip install ".[embeddings]"`. The system works without it but matches less
well.

**Database locked (SQLite).** Only run one instance per database file. For
multiple instances, use Postgres.

**Want more speed.** Lower `scan.priority_interval_seconds` and tag more
companies `priority: high`. Below about 60 seconds you risk rate limiting; the
per-host limiter will throttle you before a site does.
