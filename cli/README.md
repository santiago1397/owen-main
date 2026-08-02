# `owen` CLI

A thin wrapper over the [OWEN AI API](../backend/app/api/ai/AI_API.md), for agents that have a
shell rather than an HTTP client — and for humans debugging production.

Standard library only. Python 3.9+. Nothing to install.

## Setup

```bash
export OWEN_API_URL=https://api.owen.santiagoproperties.uk
export OWEN_API_KEY=owen_sk_...        # issue one in the OWEN UI under API Keys
```

Or write `~/.owen/config.json`:

```json
{ "url": "https://api.owen.santiagoproperties.uk", "key": "owen_sk_..." }
```

Optionally put it on your PATH:

```bash
ln -s "$PWD/cli/owen.py" ~/.local/bin/owen && chmod +x cli/owen.py
```

## Use

```bash
owen docs                                     # the full manual — read this first
owen index                                    # machine-readable endpoint list

owen calls --period last_week                 # how many calls last week
owen calls --period yesterday --max-duration 45   # ...under 45 seconds
owen calls --period last_month --group-by campaign
owen calls --period last_30d --group-by hour_of_day

owen leads --period this_week                 # new AHS/Dispatch leads
owen leads --period last_90d --group-by week

owen health                                   # is anything broken right now
owen errors --since 6h                        # what went wrong
owen errors --since 24h --service worker --level ERROR

owen recent --period today --table            # calls with AI summaries
owen transcript <call_id>                     # what was said

owen billing --period last_month --group-by number
owen schema --table calls                     # before writing SQL
owen query "SELECT count(*) FROM calls WHERE started_at IS NOT NULL"
echo "SELECT ..." | owen query -               # or from stdin
```

Output is JSON by default so it parses predictably; add `--table` for human-readable output.

## Notes

- Everything is **read-only**; nothing here can change OWEN's data.
- `recent`, `transcript` and `leads-recent` need the `content` scope; `errors` needs `logs`;
  `query` needs both `sql` and `content`.
- Exit codes: `0` success, `1` API error (the error body, including its `hint`, is printed to
  stderr), `2` usage or configuration error.
- **Always mind the `notes` field.** A call count quoted without its filters is a wrong number —
  see the three caveats at the top of the manual.
