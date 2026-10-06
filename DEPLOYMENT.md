# Deploying to AWS Lightsail + RDS

This project runs as a set of cron-triggered jobs (`scheduler/runner.py`),
reading `scheduled_jobs` from Postgres on every tick and dispatching to the
fetch scripts in `scheduler/jobs.py`. You already have a Lightsail instance
and a separate RDS Postgres instance — this guide wires them together, gets
the code running unattended, and gives you a crontab line plus log rotation.

Every script now logs through `logging_setup.py` to both stdout and a daily
rotating file under `logs/` (gitignored, 14 days retention). See the bottom
of this doc for where to look when something fails.

## 1. Network: let the Lightsail instance reach RDS

Lightsail instances are **not** in the same VPC as RDS by default, so RDS has
to either be reachable over the public internet (with a tightly scoped
security group) or reachable via Lightsail VPC peering. The simplest path:

1. In the RDS console, confirm the instance's **Public accessibility** is
   `Yes` (if it's `No`, use VPC peering instead — see note below).
2. Get the Lightsail instance's **static IP** (Lightsail console → your
   instance → Networking tab → attach a static IP if it doesn't have one
   yet — without one, the IP changes on every reboot/stop, which would
   silently break the RDS security group rule).
3. In the RDS security group, add an inbound rule: **Type** PostgreSQL,
   **Port** 5432, **Source** `<lightsail-static-ip>/32`. Do not use
   `0.0.0.0/0`.

**If RDS is not publicly accessible:** enable VPC peering instead
(Lightsail console → Account → Advanced → "VPC peering" → enable). This peers
*all* your Lightsail instances with your default VPC in that region, after
which the instance can reach RDS via its private endpoint and the security
group rule should reference the Lightsail peered VPC's CIDR instead of a
public IP.

## 2. Instance prep

SSH into the Lightsail instance, then:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11-venv git
```

`psycopg2-binary` is already in `requirements.txt`, so no Postgres client
build dependencies (`libpq-dev`) are needed.

## 3. Get the code onto the instance

```bash
git clone <your-repo-url> alphaDecodeFinancialTechCrons
cd alphaDecodeFinancialTechCrons
```

If the repo is private, use a deploy key (read-only, scoped to this repo) or
a personal access token — don't reuse your own full-access GitHub credentials
on the server.

## 4. Environment

Create `.env` **on the server** (never commit this file — it's already
gitignored):

```bash
DATABASE_URL=postgresql+psycopg2://<user>:<password>@<rds-endpoint>:5432/<dbname>
GDFL_ACCESS_KEY=...
RESEND_API_KEY=...
NOTIFY_FROM_EMAIL=...
NOTIFY_RECIPIENTS=you@example.com,ops@example.com
```

Optional logging overrides (defaults are fine for most cases):

```bash
LOG_LEVEL=INFO     # DEBUG/INFO/WARNING/ERROR
LOG_DIR=logs       # relative to the repo root, or an absolute path
```

## 5. Install

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## 6. Initialize the scheduler's tables

```bash
.venv/bin/python -m scheduler.setup_tables
```

This creates `scheduled_jobs`/`job_run_log` (idempotent, safe to re-run) and
seeds the 5 default jobs on the RDS database. Check `logs/setup_tables.log`
for confirmation.

## 7. Smoke test before trusting cron

Run it manually once, as if a job's scheduled time had just passed:

```bash
.venv/bin/python -m scheduler.runner --now "$(date +%Y-%m-%d) 16:16"
```

Confirm:
- `logs/scheduler.log` shows the tick and any job(s) that ran.
- The relevant DB tables got updated (check via `psql` or your DB client).
- If `RESEND_API_KEY`/`NOTIFY_FROM_EMAIL`/`NOTIFY_RECIPIENTS` are set, an
  email arrived for the job's outcome.

Only move to cron once this works end to end.

## 8. Crontab

The 5 default jobs are spread across specific times in the day (16:15,
16:20, 16:35, 23:30, 08:30 IST) — `scheduler/runner.py` is designed to be
ticked every few minutes and only acts on jobs whose `run_time` has passed
and haven't run yet today. Every 5 minutes is a good balance of promptness
vs. load:

```bash
crontab -e
```

Add:

```cron
*/5 * * * * cd /home/ubuntu/alphaDecodeFinancialTechCrons && /usr/bin/flock -n /tmp/scheduler_runner.lock /home/ubuntu/alphaDecodeFinancialTechCrons/.venv/bin/python -m scheduler.runner >> /home/ubuntu/alphaDecodeFinancialTechCrons/logs/cron.out 2>&1
```

Notes on this line:
- `flock -n` prevents two overlapping runs if one tick ever takes longer than
  5 minutes (e.g. a slow API).
- Absolute paths everywhere — cron's `PATH`/`cwd` are minimal and won't find
  the venv's `python` or resolve relative paths the way an interactive shell
  does.
- `cron.out` is a crash-before-logging-initializes fallback only — the real,
  structured, rotated log is `logs/scheduler.log` (written by
  `logging_setup.py`). `cron.out` has no rotation of its own, so add a
  logrotate entry for it:

```bash
sudo tee /etc/logrotate.d/alphadecode-cron <<'EOF'
/home/ubuntu/alphaDecodeFinancialTechCrons/logs/cron.out {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
EOF
```

## 9. Firewall

Lightsail console → your instance → Networking tab → only port 22 (SSH)
should be open, ideally restricted to your own IP rather than `0.0.0.0/0`.
There's no web server here, so nothing else needs to be exposed.

## 10. Resilience

- Enable **automatic snapshots** for the Lightsail instance (Lightsail
  console → instance → Snapshots tab) in case the VM itself is lost.
- RDS automated backups are Amazon's responsibility once enabled on the RDS
  instance — confirm backup retention is set to something reasonable (7+
  days) in the RDS console.

## Tracing issues after deployment

| Symptom | Where to look |
|---|---|
| A scheduled job didn't run / ran with wrong data | `logs/scheduler.log` on the server — every tick, job start/skip/result is logged with a timestamp. |
| A job failed | `logs/scheduler.log` has a full traceback (`logger.exception`) at the failure point; the same traceback is in the `job_run_log.error_detail` DB column; an email went out if `notify_on_failure` is set for that job. |
| A manually-run script misbehaved | `logs/<script_name>.log` (e.g. `logs/fetch_historical_financials.log`) — each entry point writes its own file. |
| Cron didn't run at all | `logs/cron.out` — if this is empty/stale, cron itself isn't firing; check `sudo systemctl status cron` and `grep CRON /var/log/syslog`. |
| Nothing in any log | The process likely crashed before `configure_logging()` ran (e.g. missing `.env`/import error) — check `logs/cron.out` first, since stdout/stderr land there even pre-logging-setup. |
