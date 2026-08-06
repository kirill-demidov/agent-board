# Agent Board

> ### This is a fork
>
> Forked from **[mikky-a/agentboard](https://github.com/mikky-a/agentboard)**,
> MIT-licensed, copyright held by its author — see [LICENSE](LICENSE). Upstream
> is the original and the place to look for releases; this fork exists to
> scratch a few personal itches and is not a competing product.
>
> **What is different here**
>
> - **Cross-origin requests to the local API are refused.** The board binds
>   127.0.0.1, but a browser hands that origin to any open tab, and every
>   mutating route is a plain GET — so a web page could start an agent in your
>   repo or type into a live session. Rejected by `Sec-Fetch-Site`, `Origin`
>   and `Host` (DNS rebinding). Worth pulling upstream.
> - **Sessions started outside the board are visible.** Claude registers live
>   sessions in `~/.claude/sessions`, so agents launched by hand in a terminal,
>   VS Code or Cursor appear as read-only cards labelled with the app hosting
>   them — and can be taken over: the board kills the external process and
>   resumes the same conversation in a tmux session it controls.
> - **Full-text search over every conversation**, reusing the FTS5 index kept by
>   a local `~/.claude/cc-history` tool — opened read-only, refreshed by running
>   that tool's own indexer, and absent without it. A hit opens in one click.
> - **Both ages on a card** — how long the process has run and how long the
>   conversation has existed, which stop matching once a session is taken over.
> - **A card centres and grows while you talk to it**, instead of typing into a
>   280px tile wherever it happens to sit.
> - **A tidy button** packs drifted cards into columns by project.
> - **The amber counter is a switch**: click it and the board keeps only the
>   sessions waiting for you — a card you are answering stays put until you
>   close its composer, and the filter releases the board once nobody waits.
> - **Card size** — compact / normal / large / huge, in settings; the layout math
>   follows, so a resize repacks the grid instead of overlapping tiles.
> - **`AGENTBOARD_TERMINAL`** picks the app that opens a session; with the
>   board's `~/.zshrc` snippet Warp opens sessions as tabs, otherwise as windows.
> - **`AGENTBOARD_SELFNAME=0`** keeps the board out of the agent's global memory
>   (`~/.claude/CLAUDE.md` and friends) at the cost of derived card titles.
> - Fixes: cyrillic no longer arrives as mojibake when sent from the board (the
>   packaged app inherits no `LC_*` from Finder); top-bar buttons no longer get
>   pushed off screen by a long row of project chips; a server that dies after
>   the page has loaded is noticed and restarted.
>
> Packaged builds bundle CPython (PSF), tmux (ISC), libevent (BSD-3) and
> utf8proc (MIT) — their licences apply to the DMG, not to this source tree.

**Spatial board for Claude Code, Codex, Cursor and opencode.** One card is one
conversation: start agents into any project folder, drag the cards wherever they
make sense, and see at a glance who is working, who finished, and who is waiting
for you.

![Agent Board — launching agents, statuses, replying from the board](docs/demo.gif)

**[Download AgentBoard.dmg](https://github.com/mikky-a/agentboard/releases/latest/download/AgentBoard.dmg)** ·
[agentboard site](https://mikky-a.github.io/agentboard/) · macOS 13+ · MIT

- **Start an agent in a second** — pick a folder, type the task. It runs in the
  background as a tile; no terminal window opens.
- **A card turns amber the moment an agent needs you** — the header counts how
  many are waiting, so you stop finding out forty minutes later.
- **Answer from the board** — reply straight from the card; open the live
  terminal only when you want to watch.
- **Nothing piles up** — cards survive reboots and resume with one click,
  removing a card never deletes history, and your desktop stays clean.
- **Three skins** — terminal phosphor, macOS glass, Soviet retro console, each
  with a night mode.

Everything runs locally: the board reads the CLIs' own logs and talks to tmux on
your machine. No account, no telemetry. The UI speaks English and Russian.

## Requirements

- macOS 13+ (Apple Silicon)
- Any of: [Claude Code](https://claude.com/claude-code), [Codex CLI](https://github.com/openai/codex),
  [Cursor CLI](https://cursor.com/docs/cli), [opencode](https://opencode.ai)

That's it — the app is self-contained (bundles its own Python runtime and tmux),
signed and notarized. No Homebrew, no Xcode tools.

## Quickstart

**[Download AgentBoard.dmg](https://github.com/mikky-a/agentboard/releases/latest/download/AgentBoard.dmg)**,
drag to Applications, open. The dock icon shows a badge with how many agents
are waiting for you.

<details>
<summary>Alternative: install from source (curl | sh)</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/mikky-a/agentboard/main/install.sh | sh
```

This clones the repo into `~/.agentboard`, sets up start-on-login (launchd),
installs tmux if missing (via Homebrew), builds **AgentBoard.app** with your
Xcode Command Line Tools into `~/Applications` and opens it; without `swiftc`
it falls back to the browser at `http://localhost:8787`. Updates ride on
`git pull` — re-run the same command anytime.

</details>

Prefer to run things by hand?

```bash
git clone https://github.com/mikky-a/agentboard.git
cd agentboard
python3 agentboard.py        # server → http://localhost:8787
./build_app.sh               # native app (optional)
```

Open the board in a browser. If status hooks are not installed yet, a banner
appears in the top bar — click it once. It idempotently adds lifecycle hooks
for every CLI you have installed (backing up your originals as
`*.agentboard-bak`): `~/.claude/settings.json`, `~/.codex/hooks.json`,
`~/.cursor/hooks.json` (plus a `Shell(tee)` allowlist entry — Cursor's CLI
ignores hook permission responses), and an opencode plugin in
`~/.config/opencode/plugins/`. Restart any live agent sessions after
installing; Codex will ask to trust the new hooks — choose "Trust all and
continue". When you create an agent from the board, the folder is pre-trusted
in the CLI's own config (Claude / Codex / Cursor), so agents don't silently
stall on first-run "do you trust this directory?" dialogs.

## Statuses

🟢 working · 🟡 waiting for you (blinks + sound) · ⚪ idle · 🔵 paused

Statuses come from the CLIs' own lifecycle hooks (e.g. the `PermissionRequest`
event) writing to `~/.claude/agent-status/<tmux-session>` — no fragile parsing
of terminal output.

## Native app

`install.sh` builds it automatically; to rebuild by hand: `./build_app.sh`
(swiftc + icon → AgentBoard.app). The dock badge shows how many agents are
waiting for you — the board lives in the Dock, not in a browser tab.

## Updates

The board checks GitHub Releases once a day; when a new version is out, a
badge appears in the top bar — one click runs `git pull` and restarts the
server. (Installed via `install.sh` / `git clone` — that's what makes the
pull possible.)

## Configuration

- `AGENTBOARD_DIRS` — colon-separated list of folders to scan for projects in
  the "＋ agent" picker (default: `~/Documents/dev:~/Documents`). Any other
  folder is always reachable via the native "other folder…" dialog.
- `AGENTBOARD_TERMINAL` — which app "open" hands the session to (default:
  `Terminal`). `Warp` is special-cased: it neither opens `.command` files nor
  speaks AppleScript. With the board's `~/.zshrc` snippet in place (installed
  together with the hooks) sessions open as a **tab** in the current Warp
  window: the board drops the `tmux attach` line into
  `/tmp/agentboard-<uid>-warp-attach` and follows
  `warp://action/new_tab?path=<cwd>`, and the new tab's shell picks that line up
  and `exec`s it. Without the snippet it falls back to a launch configuration in
  `~/.warp/launch_configurations/` plus `warp://launch/<name>`, which Warp can
  only open as a new window. Any other
  value is passed to `open -a`, which works for anything that runs `.command`
  files (iTerm2, for one). Only `Terminal` can raise the exact window of an
  already-attached session; the rest just come to the front.
- `AGENTBOARD_SELFNAME=0` — turn off card self-naming. By default the board
  appends an `## Agent Board (meta-harness)` section to the agent's global
  memory (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`,
  `~/.config/opencode/AGENTS.md`) asking it to `tee` a short task title as its
  very first action, so cards get meaningful names. With `0` the board writes
  nothing to those files and sends no naming instruction; card titles fall back
  to the first prompt and the session summary.

## Files

- `agentboard.py` — server (Python stdlib, localhost:8787)
- `index.html` — the whole UI
- `skins/` — macOS and Soviet themes on top of the base terminal one
- `board.json` — your board state (created on first run, not in the repo)
- `app/` + `build_app.sh` — native wrapper (Swift + WKWebView)

## Roadmap

- Resume for paused Codex / Cursor / opencode cards (Claude only for now)
- Homebrew tap
- Windows/Linux support

## License

MIT
