# herdr-config

A [Herdr](https://herdr.dev) configuration that turns the expanded agent
sidebar into a live usage dashboard, plus a small Python reporter
(`session-usage.py`) that reads each coding agent's own session files.

Works with **OpenCode**, **pi**, **Claude Code**, and **GitHub Copilot**.
Agents without a known session format still get a status glyph and state
word in the sidebar.

## What you get

Sidebar agent rows (configured by `[ui.sidebar.agents]` in `config.toml`):

| Row | Content |
|---|---|
| 1 | status glyph (`✳` working, `⋯` background, `!` blocked, `✓` done, `○` idle, `?` unknown), model, workspace |
| 2 | agent kind, state word (colored) |
| 3 | `ctx` (context used), `cost` (session total), `ch` (cache hit rate) |

The tab bar also shows live CPU/RAM (`status.sh`), hostname, and the time.

### Support matrix

| Agent | State/icon | model | ctx | cost | cache hit |
|---|---|---|---|---|---|
| OpenCode | yes | yes | yes | USD | yes |
| pi | yes | yes | yes | USD | yes |
| Claude Code | yes | yes | yes | USD | yes |
| GitHub Copilot | yes | yes | yes | AI credits (`cr`) or premium requests (`req`) | yes |
| any other | yes | – | – | – | – |

`ctx` is the token count of the latest model call, the same number each
CLI's own footer shows. `ch` is `cacheRead / (input + cacheRead +
cacheWrite)` of the latest call. Copilot does not bill in dollars, so its
cost token shows credits (or `Nreq` when only premium requests are
reported).

## Requirements

- Herdr with `herdr integration` support (tested with 0.9.5).
- Python 3.8+ (standard library only, no packages).
- The CLI of every agent you want to track, installed and used at least
  once so its config/data directory exists.

## Install

```sh
git clone <this repo> ~/Code/herdr-config
cd ~/Code/herdr-config
./install.sh claude copilot        # or: ./install.sh pi opencode
```

The installer:

1. backs up and copies `config.toml`, `session-usage.py`, and `status.sh`
   into `~/.config/herdr/`;
2. writes and loads the launchd agent
   `com.<your-user>.herdr-session-usage` (macOS);
3. runs `herdr integration install <agent>` for every agent argument.
   Herdr refuses when the agent's config directory is missing, so install
   the agent first;
4. reloads the Herdr config (`herdr server reload-config`).

Without arguments, `./install.sh` only copies the config files, so you
can install the Herdr integrations yourself:

```sh
herdr integration install claude
herdr integration install copilot
herdr integration install pi
herdr integration install opencode
```

### Verify

```sh
herdr integration status                       # all agents: current?
python3 ~/.config/herdr/session-usage.py --once  # one line per pane
```

`--once` prints the tokens reported for each pane, for example:

```
w3:pK <- icon=✳ state=working model=claude-sonnet-4-5 cost=$1.42 ctx=68.3K ch=CH:97.6%
```

In Herdr, restart a running agent once after installing its integration
so it picks up the hook or extension, and give long-running panes a fresh
turn so the reporter has usage to read.

## How it works

Every 5 seconds the reporter:

1. runs `herdr agent list` to find panes, their agent kind, state, and
   session reference;
2. resolves that reference to the session file for the agent and computes
   model, context tokens, cumulative cost, and the latest cache hit rate;
3. pushes the values back to Herdr:

   ```sh
   herdr pane report-metadata <pane> --source session-usage \
     --token icon=✳ --token state=working --token model=... \
     --token ctx=68.3K --token cost='$1.42' --token ch=CH:97.6% \
     --ttl-ms 15000
   ```

The TTL makes the tokens vanish when the reporter stops, so stale values
never stick. For OpenCode the reporter also upgrades a stale `idle` to
`working` or `background` while a turn, a subagent, or a background shell
is still alive.

### Session data sources

| Agent | Session reference from Herdr | Data read |
|---|---|---|
| OpenCode | session id | `~/.local/share/opencode/opencode.db` plus the OpenCode HTTP API for live turns/subagents/shells |
| pi | transcript path | `~/.pi/agent/sessions/<cwd>/<timestamp>_<id>.jsonl` |
| Claude Code | transcript path (id fallback) | `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`; assistant `message.usage`, cumulative `cost-state.totalCostUSD`, per-message `costUSD` on older versions |
| GitHub Copilot | session id | `~/.copilot/session-state/<session-id>/events.jsonl`; durable `session.usage_record` receipts and `session.usage_checkpoint` snapshots (live `assistant.usage` events are not persisted by Copilot) |

`CLAUDE_CONFIG_DIR` and `COPILOT_HOME` are honored when set.

## Customizing

Edit the `[ui.sidebar.agents]` rows in `~/.config/herdr/config.toml`.
Alongside Herdr's built-in fields (`agent`, `workspace`, `branch`, ...),
this setup supplies the custom tokens `$icon`, `$state`, `$model`, `$ctx`,
`$cost`, and `$ch`. Colors come from `rules`, for example:

```toml
[{ token = "$cost", rules = [{ starts_with = "$", fg = "#f9e2af" }] }, "$ctx", "$ch"]
```

Validate and apply changes:

```sh
herdr config check
herdr server reload-config
```

## Linux (systemd)

`install.sh` only creates the launchd agent on macOS. On Linux, run the
reporter as a systemd user service:

```sh
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/herdr-session-usage.service <<'EOF'
[Unit]
Description=Report agent session usage to Herdr

[Service]
ExecStart=%h/.config/herdr/session-usage.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now herdr-session-usage
```

## Troubleshooting

- **A pane shows state but no model/ctx/cost.** The agent has no
  `agent_session` yet. Check `herdr integration status`; reinstall the
  integration with `herdr integration install <agent>` and restart that
  agent. For Copilot, the CLI must be new enough to persist usage events.
- **Tokens stop updating.** Check the reporter:
  `launchctl kickstart -k gui/$(id -u)/com.$(id -un).herdr-session-usage`
  and read `~/.config/herdr/session-usage.log`. After changing
  `session-usage.py`, re-run `./install.sh` and kickstart the agent.
- **Cost shows `0.00cr` for Copilot.** Credits accrue per model call;
  brand-new or state-only sessions have none yet.
- **Claude cost is missing.** Very old transcripts have neither
  `cost-state` nor `costUSD` entries; the other fields still work.
- **Herdr does not find `herdr` in the reporter log.** Adjust the `PATH`
  in the launchd plist (or your systemd unit).

## Security

The repository contains no credentials or API keys. `session-usage.py`
only reads local session files and talks to the local Herdr socket.
Session files and logs stay out of the repo (see `.gitignore`).
