# Sikka Sync Agent v4.0 — Setup Guide


## 0. Before anything: rotate your Supabase key

The `service_role` key embedded in the old `sync_agent.py` is now considered
compromised — it's been sitting in plain text in a file distributed to
merchant computers. In the Supabase dashboard:
`Project Settings → API → reset service_role key`, then update wherever
your dashboard/backend currently uses the old one. Do this before deploying
v4.0 anywhere.

Longer term, read the `SECURITY NOTE` at the bottom of `sync_agent.py` —
the real fix is to stop using `service_role` in a field-deployed agent at
all, in favor of a scoped per-tenant token validated server-side.

Also worth knowing: if the *old* `sync_agent.py` (with the key hardcoded)
was ever pushed to this repo's `main`/`master` branch, that key still lives
in git history even after you delete it from the current file. Rotating
the key neutralizes that specific leak; if the repo is or ever becomes
public, you'd also want to scrub history (`git filter-repo` or the BFG
Repo-Cleaner) to remove it properly. Nothing in v4.0 puts a secret in the
repo going forward — `.env`, `license.key`, and `db_config.json` are all in
`.gitignore` — so this is a one-time cleanup for the old leak, not an
ongoing risk.

## 1. Install dependencies

```
pip install -r requirements.txt
```

`pyodbc` also needs the "ODBC Driver 17 for SQL Server" installed on the
machine (same as before — no change there).

## 2. Configure secrets

Copy `.env.example` to `.env` in the same folder as `sync_agent.py`, and
fill in `SUPABASE_URL` and `SUPABASE_KEY`. Adjust `SIKKA_SYNC_INTERVAL` if
60 seconds is more or less than you need.

## 3. First run (interactive, one time per machine)

```
python sync_agent.py
```

This is the same activation flow as before — tenant ID, SQL Server host,
and SQL credentials — but it no longer auto-creates the `SAGEREADER` SQL
login for you. Create that login once, manually, in SQL Server Management
Studio, with a unique password and **read-only** permissions on the Sage
database. Automating "create the same login with the same hardcoded
password" across every install means every merchant site shares one DB
credential — if one leaks, they all do.

Once activation succeeds, `license.key` and `db_config.json` are written
next to the script, and the agent starts syncing on its configured
interval. Stop it with Ctrl+C to confirm it shuts down cleanly (you should
see "Arrêt propre de l'agent." in the log).

## 4. Make it run automatically (auto-start, survives reboots/crashes)

You have two options. Both assume step 3 has already been completed once
(so `license.key` exists and no interactive prompt is needed anymore).

### Option A — Windows Service via NSSM (recommended)

A real Windows service: starts at boot before any user logs in, restarts
itself if the process crashes, and is manageable from `services.msc`.

1. Download `nssm.exe` from https://nssm.cc/download and place it in this
   same folder.
2. Open PowerShell **as Administrator**, `cd` into this folder, and run:
   ```
   .\install_service.ps1
   ```
3. Verify: `Get-Service SikkaSyncAgent` should show `Running`.

### Option B — Task Scheduler (no extra download)

If you'd rather not use a third-party tool:

1. Open Task Scheduler → Create Task.
2. General tab: name it `SikkaSyncAgent`, check "Run whether user is
   logged on or not", check "Run with highest privileges".
3. Triggers tab: New → "At startup".
4. Actions tab: New → Program: path to `python.exe`, Arguments:
   `sync_agent.py`, Start in: this folder's path.
5. Settings tab: check "If the task fails, restart every" → 1 minute,
   up to a high retry count; uncheck "Stop the task if it runs longer
   than" (it's meant to run forever).

Task Scheduler is a fine fallback, but a real service (Option A) gives you
cleaner crash-restart behavior and centralized log/status management.

## 5. What changed vs. v3.4 — quick summary

- No secrets in source (`.env` instead of hardcoded strings).
- Logging to a rotating file in `logs/`, not just stdout.
- Retries with backoff on both the SQL Server and Supabase calls, and a
  circuit breaker that backs off further after repeated failures instead
  of retrying every 60s forever.
- Single-instance lock — a second launch (e.g. Task Scheduler firing while
  you're also running it manually) refuses to start instead of double-
  syncing.
- Heartbeat: after every cycle the agent writes `status`,
  `last_sync_duration_ms`, and `last_sync_error` to the `merchants` table,
  so your dashboard can show real sync health instead of a static badge.
  You'll need to add those columns to `merchants` if they don't exist yet:
  ```sql
  alter table merchants
    add column if not exists last_sync_duration_ms integer,
    add column if not exists last_sync_error text;
  ```
- Graceful shutdown on Ctrl+C / service stop.
- Auto-creation of the `SAGEREADER` login with a hardcoded password has
  been removed — create it manually, once, per site.

## 6. Building the .exe via GitHub Actions

Push this whole folder to a repo (`main` or `master`). The workflow in
`.github/workflows/build-exe.yml` runs automatically on push, or you can
trigger it manually from the repo's **Actions** tab via "Run workflow".

It builds `sync_agent.exe` using `sync_agent.spec` (not raw `--onefile`
flags) — the spec is what pulls in the hidden imports supabase-py needs
(`gotrue`, `postgrest`, `realtime`, `storage3`, `supafunc`); building
without it risks an exe that launches fine but fails the first time it
talks to Supabase.

Two artifacts come out of each run, downloadable from the workflow's
summary page:

- **`sync-agent-exe`** — just the exe, if that's all you need.
- **`sikka-sync-agent-release`** — the exe plus `.env.example`,
  `README.md`, and `install_service.ps1` zipped together, ready to hand to
  whoever's setting up a new merchant machine.

No GitHub secrets are needed for this build — v4.0 doesn't bake any
Supabase key into the binary. Each machine gets its own `.env` dropped
next to the exe after the build (see step 2), which is exactly why the
`.env`/license/db-config files are gitignored and never part of the
release zip.

The exe is unsigned. Windows SmartScreen or Defender may warn on first run
on a new machine — that's expected for any unsigned binary, not a sign of
a bad build. A code-signing certificate would remove that warning if it
becomes a recurring friction point with merchants.
