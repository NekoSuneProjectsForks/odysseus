# Odysseus — Feature TODO / Ideas

Working notes from a codebase pass, answering "what's already here" and laying
out ideas for: Discord, GitHub/coding-agent workflows, image gen, an in-app
coding assistant, VRChat/VRCX, and general gaming/content/dev extras.

Nothing below is committed to — it's a menu to prioritize from.

**Update:** §1 (Discord), §2 (GitHub), §3 (image backends), and §4 (coding
assistant) have since been implemented — see the "BUILT" markers in each
section for what exists now vs. what's still a follow-up. §0 below still
describes the *pre-session* starting state (kept as-is for context on why
these were built the way they were). §5 (VRChat) and §6 remain untouched,
as decided.

---

## 0. What Odysseus already has (read before building any of this)

- **Agent core**: `src/agent_loop.py`, `src/tool_execution.py`, `src/tool_implementations.py`,
  `src/mcp_manager.py`, `src/builtin_mcp.py` — a real tool-calling agent loop with
  MCP server support (`mcp_servers/*.py`: email, image_gen, memory, rag).
- **Shell execution**: `routes/shell_routes.py` — admin-only, real PTY/tmux,
  cross-platform (incl. Windows via git-bash). This means the agent already has
  the raw capability to run `git clone/commit/push`, run builds/tests, install
  deps, etc. — **the primitive for "build code" already exists**; there's just
  no dedicated repo/PR workflow UI wrapping it yet (see §2).
- **Generic Integrations system**: `src/integrations.py` + `routes/*` + `static/js/settings.js`.
  A user can register any REST API (base URL + auth type: header/bearer/basic/query),
  and the agent calls it via an `api_call` tool. Presets exist for Miniflux, Gitea,
  Linkding, Home Assistant, ntfy, **Discord Incoming Webhook (outgoing/post-only)**,
  Vaultwarden, FreshRSS. Secrets are encrypted at rest (`src/secret_storage.py`).
  SSRF-guarded outbound requests (`src/url_safety.py`).
- **Image generation — already built**: `mcp_servers/image_gen_server.py`. Calls
  any OpenAI-compatible `/images/generations` endpoint (gpt-image-1, gpt-image-1.5,
  dall-e-3), auto-detects model, saves to the Gallery DB, returns a direct link.
  Size/quality validated per-model. **No self-hosted (SD/ComfyUI/A1111) backend yet.**
- **External coding-agent companions**: `integrations/claude/` and `integrations/codex/`
  — these are the *reverse* direction from what's being asked for. They let an
  external Claude Code / Codex CLI session (running in a terminal, outside Odysseus)
  authenticate with a scoped API token and call back into Odysseus's
  `/api/codex/*` endpoints for todos, email, memory, calendar, docs, cookbook.
  They do **not** give Odysseus itself a "write code in a cloned repo" workflow —
  that's a separate, new feature (§2/§4).
- **Discord**: outgoing webhook posting only (`discord_webhook` preset above). No
  bot/gateway connection anywhere in the repo — can't read messages, history,
  member counts, or presence today.
- **VRChat**: nothing in the repo (`grep -ri vrchat` = zero hits).
- **GitHub**: only test/doc/CI references (Actions workflows, dependabot). No
  OAuth/PAT-based repo integration, no clone/push/PR system.
- **Stack**: Python/FastAPI/SQLAlchemy backend, vanilla JS frontend, Docker-first
  deploy (`docker-compose.yml`), also native Windows/macOS launchers.

---

## 1. Discord Bot Integration — BUILT

- [x] **Bot architecture**: `services/discord/service.py` (`DiscordBotService`),
  started/stopped from `app.py`'s lifespan hooks alongside STT/TTS-style
  services. discord.py is an **optional dependency** (`requirements-optional.txt`),
  lazily imported — the app boots fine and the panel shows "not installed"
  until it's added.
- [x] Settings > Integrations > **Discord Bot** panel (`static/index.html` +
  `static/js/settings.js` `initDiscordIntegration()`): paste bot token
  (encrypted via `EncryptedText`/`src/secret_storage.py`), optional default
  guild id, connect/disconnect, live status. In-app copy calls out the
  Message Content + Server Members privileged intents requirement.
- [x] Read messages / message history — `channel.history()` live against
  Discord's API (no local cache table needed; simpler and always current).
- [x] Search message history by keyword, optionally scoped to one channel —
  `DiscordBotService.search_messages`.
- [x] Guild member count + online/idle/dnd/offline breakdown —
  `guild_summary`/`list_members`.
- [x] Send messages from Odysseus — `send_message`, wired to both the UI and
  the agent tool.
- [x] Exposed as one agent tool `manage_discord` (actions: status,
  list_guilds, guild_summary, list_members, list_channels, search_messages,
  send_message) — `src/agent_tools/discord_tools.py`. Admin-only: added to
  `NON_ADMIN_BLOCKED_TOOLS` and the plan-mode mutator list in `src/tool_security.py`.
- [x] Rate-limit / backoff handling for Discord API (429s) — confirmed
  discord.py's HTTP client already tracks rate-limit buckets and retries on
  429/`Retry-After` internally for every call our service makes (history,
  member/guild fetch, send). No custom logic needed; revisit only if it
  becomes a problem in practice.
- [ ] Extend `THREAT_MODEL.md` / `SECURITY.md` with the new Discord bot token
  credential class (not yet done — flagging as a follow-up).

## 2. GitHub Integration — clone / edit / push / PR ("like Claude/Codex do it") — BUILT

Built as a **Settings > Integrations > GitHub** panel rather than a new
top-level "Projects" nav section — kept consistent with how every other
integration (Email, MCP, CalDAV, Vault, ...) lives in that one tab, and
avoided introducing a new top-level nav pattern in one pass. Revisit a
dedicated "Projects" section later if the repo list UI outgrows a settings panel.
**Admin-only, full stop** (see §7) — enforced via `require_admin` on every
route and `NON_ADMIN_BLOCKED_TOOLS`/plan-mode mutators for the agent tool.

- [x] **Auth — GitHub OAuth App**, not PAT. `routes/github_routes.py`
  `/oauth/authorize` + `/oauth/callback`, mirroring the existing Google email
  OAuth flow (`routes/email_routes.py`) incl. signed CSRF state
  (`routes/email_helpers.py` `make_oauth_state`/`verify_oauth_state`, reused
  as-is). Client id/secret/redirect URI via env vars, documented in `.env.example`.
  Access token stored via `EncryptedText` on `core.database.GithubAccount`.
  Token refresh: not needed for v1 — GitHub OAuth Apps issue non-expiring
  user tokens by default; revisit only if the app later opts into expiring tokens.
- [x] **Repo creation**, including org repos and creating from a fresh name —
  `src/github_service.py` `create_repo()` (auto-inits with a README on GitHub,
  then clones locally into the same workspace flow as any other tracked repo).
- [x] **"Upload existing project"** — `upload_existing()`: creates the GitHub
  repo (no auto-init), `git init`s the local folder if needed, commits
  anything uncommitted, sets the remote, and pushes.
- [x] **Repo workspace model** — `core.database.GithubRepo` (full_name,
  remote_url, local_path, default_branch, private, origin: cloned/created/uploaded).
  Working copies live under `GITHUB_REPOS_DIR` = `data/repos/` (`src/constants.py`).
  Git ops run via `asyncio.create_subprocess_exec("git", ...)` (argv list, never
  `shell=True`) in `src/github_service.py` — a fresh, minimal wrapper rather than
  reusing the PTY-based `services/shell` (that one's built for interactive
  tmux sessions; repo git commands just need argv + stdout/stderr capture).
- [x] **Repo list UI**: create/clone/upload forms, per-repo actions (status,
  diff, commit, push, new branch, open PR, remove) in the GitHub settings card.
- [x] **Agent coding loop** — no separate implementation needed. Discovered the
  agent already has a per-turn **workspace** concept (`src/tool_execution.py`
  `agent_cwd()`/`get_active_workspace()`, set via `static/js/workspace.js`)
  that confines the existing bash/read_file/write_file/edit_file/apply_patch/
  ls/glob/grep tools to a folder. The GitHub panel's **"Open in Agent"** button
  calls `vetAndSetWorkspace(repo.local_path)` directly — one click turns any
  tracked repo into exactly the "acts like Claude Code/Codex" experience,
  reusing 100% of the existing tool suite. See §4.
- [x] **Diff review** — `repo_diff()` shows the unified `git diff` (plus
  untracked files) before commit. Scoped to "show the diff", not full
  hunk-level approve/reject — that's a real frontend undertaking (proper diff
  viewer with per-hunk checkboxes) that's a reasonable follow-up, not done here.
- [x] **Commit + push** — `commit_changes()` / `push_changes()`, exposed in
  both the UI and the `manage_github` agent tool.
- [x] **Pull request creation** — `open_pull_request()`: pushes the current
  branch, then `POST /repos/{full_name}/pulls`.
- [x] **PR/CI checks** — `pr_checks()` wraps `GET /repos/{full_name}/commits/{ref}/check-runs`.
  Exposed via the agent tool and the route; not yet in the settings UI (no
  pressing need without an open-PRs list view — add alongside a future PR list).
- [x] **Agent tool** `manage_github` (actions: status, list_repos,
  list_remote_repos, create_repo, clone_repo, upload_repo, delete_repo,
  repo_status, repo_diff, commit, push, create_branch, create_pr, checks) —
  `src/agent_tools/github_tools.py`.

**Bugs found and fixed while building this** (pre-existing, not introduced
this session): `src/integrations.py` had been overwritten with a stray JSON-ish
fragment (destroying the whole Integrations backend — `load_integrations`,
`api_call` tool, etc.); a `routes/github_routes.py` stub already existed but
imported nonexistent `dependencies`/`models` modules and was never wired into
`app.py`; `src/agent_loop.py` used `Any` in a type hint without importing it
from `typing`, which crashed the entire app at import time regardless of
these changes. All three fixed as part of this work — see git history.

## 3. Image Generation — round out what's already there — BUILT (adapters)

**Support every generation path** — OpenAI/OpenAI-compatible APIs *and*
self-hosted backends, side by side, selectable in Settings > Image Generation.

- [x] **Self-hosted backend adapters** — `src/image_backends.py`:
  - `generate_via_a1111_compatible()` — Automatic1111/SD WebUI and SD.Next
    (same `/sdapi/v1/txt2img` contract, one adapter covers both).
  - `generate_via_comfyui()` — submits a minimal default txt2img graph
    (checkpoint → CLIP encode → KSampler → VAE decode → SaveImage), polls
    `/history/{id}`, fetches the result via `/view`.
  - `mcp_servers/image_gen_server.py`'s `generate_image` tool now branches on
    the `image_backend` setting at the top — self-hosted backends short-circuit
    to the new adapters and reuse the exact same save-to-gallery/direct-link
    logic as the OpenAI path, so the agent-facing behavior is identical either way.
  - Settings UI (`static/index.html` + `settings.js` `initImageSettings()`):
    backend dropdown (openai / automatic1111 / sdnext / comfyui) with the
    relevant fields (server URL, and a checkpoint filename for ComfyUI) shown
    per backend.
- [x] Image-to-image support for the self-hosted backends (the OpenAI path's
  existing inpaint flow is untouched — this is additive). `generate_image`'s
  MCP tool schema gained `input_image_url` (an Odysseus `/api/generated-image/...`
  path or any SSRF-checked external URL) + `strength` (denoising 0-1) +
  `negative_prompt`. A1111/SD.Next: switches `/sdapi/v1/txt2img` →
  `/sdapi/v1/img2img` with `init_images`. ComfyUI: uploads the source via
  `/upload/image`, swaps `EmptyLatentImage` for `LoadImage`→`VAEEncode`, and
  sets `KSampler.denoise` from `strength`. Fetching `input_image_url` reuses
  `src/url_safety.check_outbound_url` for SSRF protection on external URLs.
  Not full inpainting (no mask input yet) — that's the natural next step.
- [x] Model/LoRA picker for self-hosted backends. New `routes/image_backend_routes.py`
  (`GET /api/image-backends/checkpoints|loras?backend=...`, admin-gated) proxies
  A1111/SD.Next's `/sdapi/v1/sd-models` + `/sdapi/v1/loras` and ComfyUI's
  `/object_info/CheckpointLoaderSimple` + `/object_info/LoraLoader` server-side
  (avoids CORS/mixed-content issues hitting an internal LAN image-gen server
  directly from the browser). Settings UI: checkpoint/LoRA fields stayed
  free-text `<input>` (so a stale/unreachable list never blocks generation)
  but gained a paired `<datalist>` + "Refresh list" button per backend for
  autocomplete. LoRA application: A1111/SD.Next append `<lora:name:1>` to the
  prompt (there's no separate payload field in that API); ComfyUI adds a
  `LoraLoader` node ahead of the sampler.
- [ ] Cost/quota guardrails for paid API backends (soft limit + admin setting) —
  not built; self-hosted backends have no per-call cost so this matters only
  for the OpenAI path, which predates this session's work.

## 4. In-app Coding Assistant ("Codex/Claude, but built in") — MOSTLY ALREADY THERE

Turned out to need almost no new code. Odysseus's existing Agent mode already
has a full tool suite (bash, python, read_file/write_file/edit_file/apply_patch,
ls/glob/grep) plus a **workspace** confinement mechanism
(`src/tool_execution.py` `agent_cwd()`, `static/js/workspace.js`) that scopes
those tools to one folder for the turn. The GitHub panel's **"Open in Agent"**
button (§2) just calls `vetAndSetWorkspace(repo.local_path)` — that alone
delivers "multi-file, real-repo, git-aware coding assistant," reusing the
entire existing tool suite rather than building a parallel one.

- [x] Confirmed Agent mode's existing file/shell tools are sufficient — the
  gap really was just "point them at a cloned repo," not new tooling.
- [x] Sandboxing — opt-in Docker container-per-workspace, built. New
  `src/bash_sandbox.py`: when Settings > Agent > "Sandboxing" is on *and* a
  workspace is active *and* Docker is reachable, `BashTool.execute()`
  (`src/agent_tools/subprocess_tools.py`) runs the command via `docker exec`
  in a long-lived container (`docker run -d ... sleep infinity`, one
  container per workspace path, keyed by a hash of its realpath so it's
  stable across restarts) that bind-mounts the workspace at `/workspace`.
  Default image `mcr.microsoft.com/devcontainers/base:ubuntu`, overridable.
  Off by default and fully additive — falls through to the existing
  unsandboxed path (unchanged) whenever the toggle is off, no workspace is
  active, or Docker isn't reachable, so nothing about the default experience
  changed. Container persists across turns (packages/build artifacts survive,
  same as a normal working directory would), but each `bash` call is still a
  fresh shell invocation — no persistent env vars between calls, matching the
  existing non-tmux fallback path's semantics. Scope note: this sandboxes
  `bash` only; it does not attempt to containerize the tmux/PTY session path,
  and there's no lifecycle UI yet for stopping/removing old sandbox
  containers (`stop_sandbox`/`remove_sandbox` exist in the module but aren't
  wired to a button — manual `docker rm` works meanwhile). `workspace.js`'s
  tooltip copy now mentions the opt-in rather than stating bash is never
  sandboxed.
- [x] Model routing: "coding model" setting, built. Mirrors the existing
  utility/teacher model settings pattern exactly (`initUtilityModel()` in
  `settings.js`) — new Settings > Agent > "Coding Model" card
  (`coding_endpoint_id` / `coding_model`, blank = same as chat). Wired into
  `routes/chat_routes.py`'s agent-mode branch: when a workspace is active,
  `src.endpoint_resolver.resolve_endpoint("coding", fallback_url=sess.endpoint_url,
  ...)` resolves the model for that turn only — the session's own model
  selection is never mutated. No new resolver code needed; `resolve_endpoint`
  already generalizes over a `setting_prefix`.
- [x] Claude Code CLI / Codex CLI as selectable AI Model endpoints — built.
  Explicitly **not** the OAuth-token-reuse shortcut that was asked for and
  declined earlier in this project's history — Odysseus shells out to the
  real `claude` / `codex` binaries (`src/claude_cli.py`, `src/codex_cli.py`),
  which authenticate themselves via their own `claude login` / `codex login`
  (or an API key) exactly as they would from an interactive terminal.
  Odysseus never touches that OAuth flow or its tokens.
  - Both fit the existing per-provider-branch pattern in `src/llm_core.py`
    via a sentinel "base URL" that's never actually dialed
    (`http://claude-code-cli.local`, `http://codex-cli.local`) —
    `_detect_provider()` recognizes them, and a new `_stream_cli_provider()`
    helper shells out to the subprocess and translates its own JSON event
    stream into Odysseus's internal SSE vocabulary (`delta`/`usage`/`error`/
    `[DONE]`). Wired into both the streaming (`_stream_llm_inner`) and
    non-streaming (`llm_call_async`) paths, plus `list_model_ids` (returns
    each CLI's model aliases — `sonnet`/`opus`/`haiku`/`opusplan` for Claude
    Code, `gpt-5.5`/`gpt-5.4`/`gpt-5.4-mini` for Codex — since neither CLI
    exposes a live `/v1/models`-style discovery endpoint).
  - Session continuation: new `claude_cli_session_id`/`claude_cli_cwd` and
    `codex_cli_session_id`/`codex_cli_cwd` columns on `sessions`
    (`core/database.py`). Each CLI's own `--resume`/`exec resume` only
    produces a coherent conversation when re-invoked from the SAME cwd it
    started in, so the resume id is only reused when the stored cwd matches
    the active workspace — otherwise a fresh CLI session starts.
  - Tool/permission scoping differs by mode: with an active Odysseus
    workspace (the GitHub-workspace coding-agent path), the CLI is allowed
    its own file/bash tools scoped to that same cwd
    (`--allowedTools ... --permission-mode acceptEdits` /
    `--sandbox workspace-write --ask-for-approval never`) so it can act as
    the primary coding engine. With no workspace (plain chat), all CLI tool
    use is denied (`--allowedTools ""` / `--sandbox read-only`) so Odysseus's
    own tool security/sandboxing stays the only tool-execution surface.
  - Docker: both CLIs install as npm globals behind the existing
    `INSTALL_OPTIONAL` build arg (`Dockerfile`); each CLI's login state
    persists via its own bind mount (`CLAUDE_CONFIG_HOST_DIR` →
    `/app/.claude`, `CODEX_CONFIG_HOST_DIR` → `/app/.codex`, matching the
    container's `HOME=/app`) across all three compose files. One-time setup
    is `docker compose exec --user odysseus odysseus claude login` / `codex
    login` — `--user` matters, a bare `exec` runs as root (HOME=/root),
    logging in under a home directory the running app never reads. See
    `.env.example`.
  - UI: "Claude Code CLI" / "Codex CLI" entries in the Add Model provider
    picker (`static/index.html`, `static/js/admin.js`) — picking one locks
    the URL to the sentinel and disables the API key field (no key needed),
    which required a small carve-out in the picker's "API key required for
    cloud providers" validation and in `routes/model_routes.py`'s
    `_probe_endpoint()` (returns the static alias list instead of trying to
    reach a fake host).
  - Known v1 limitation: Codex's `exec --json` event schema reports whole
    completed items (`item.completed` / `agent_message`) rather than
    per-token deltas the way Claude Code's `--include-partial-messages` does,
    so Codex CLI responses currently arrive in one chunk instead of
    streaming character-by-character. Functionally fine, just a coarser
    streaming experience than the other providers.
  - Fixed post-ship: `asyncio`'s default subprocess stdout line-buffer limit
    (64 KiB) was too small once real tool use was involved — Claude Code /
    Codex put a whole tool call (e.g. a full file write, big bash output) on
    one JSON line, and an unhandled `LimitOverrunError` crashed the entire
    agent run ("Agent run failed before completion"). Raised to 64 MiB in
    both `claude_cli.py`/`codex_cli.py`, with a graceful fallback if a line
    still somehow exceeds it. Also fixed a frontend bug where several
    providers' string-shaped SSE error payloads (`{"error": "<string>"}`,
    not `{"error": {"message": ...}}`) collapsed to a generic "Error 502"
    instead of the real message (`static/js/chat.js`).
  - Tool-use progress feedback — built, so coding-agent turns don't read as
    one long silent wait. Claude Code reports tool calls as whole
    `assistant`/`user` messages (tool_use / tool_result content blocks);
    Codex reports them as `item.started`/`item.completed` events
    (`command_execution`/`file_change`/`mcp_tool_call`/`web_search`/
    `plan_update`). Both are translated into Odysseus's existing
    `tool_start`/`tool_output` SSE vocabulary — the SAME one its own agent
    loop already uses — so the chat UI's tool-thread timeline (the
    expandable "ran `git status`" bubbles) works for these providers with no
    frontend changes. Required one small `agent_loop.py` fix: the main
    round-dispatch loop had no catch-all for unrecognized SSE `"type"`
    values, so these would otherwise be silently dropped. Known limitation:
    if Claude Code calls more than one tool in a single assistant message,
    only the last tool's bubble reliably pairs with its output (cosmetic;
    Odysseus's own tool-thread UI tracks one "current" bubble at a time).

## 5. VRChat Integration ("VRCX-like")

Nothing exists today — this is a from-scratch build.

- [ ] **Auth**: VRChat's API requires username/password + 2FA (TOTP or email
  code) login, no public OAuth — this means storing/handling the user's actual
  VRChat credentials. Must go through `src/secret_storage.py` encryption at
  minimum; consider whether this belongs behind an extra confirmation given how
  sensitive full-account credentials are (VRCX itself only stores a session
  cookie after interactive login, not the password).
  Also note: VRChat's API has historically been informally-supported for
  third-party tools (VRCX, vrcapi wrappers) rather than a stable public contract —
  flag potential breakage/ToS considerations to the user before building.
- [ ] Session/cookie-based auth flow (login once interactively, persist auth
  cookie, refresh like VRCX does) rather than storing the raw password for
  every call.
- [ ] Friend list + online/offline/status + current-world display.
- [ ] World/instance info lookup, join-notify.
- [ ] Avatar list / favorites browsing.
- [ ] Notification feed (friend requests, invites).
- [ ] Moderation helpers VRCX is known for (e.g. past-display-name lookup) —
  scope depends on what VRChat's API still exposes.
- [ ] This is realistically its own service module (`services/vrchat/`) + a new
  top-level UI section, similar scale to the Email feature.

## 6. Broader Gaming / Content-Creation / Dev ideas (unscoped — for later prioritization)

- [ ] Twitch integration (stream status, chat read/post) — same shape as Discord bot.
- [ ] YouTube — repo already has `services/youtube/youtube_handler.py`; check
  what it currently does before assuming a gap.
- [ ] Steam library / friends status via Steam Web API.
- [ ] OBS control (obs-websocket) for stream automation triggered by agent/notes/tasks.
- [ ] Screenshot/clip organizer feeding into the existing Gallery.

## 7. Security posture — decided

**Admin-only, across the board.** Discord bot config/data, GitHub OAuth +
repo/push access, and VRChat login/session are all admin-only surfaces — no
other account role gets any access to these features or the data behind them.
Concretely this means:

- [x] Every new route for GitHub/Discord goes behind `core.middleware.require_admin`
  (the canonical shared helper — used instead of `routes/shell_routes.py`'s
  local, non-reused copy) — every handler in `routes/github_routes.py` and
  `routes/discord_routes.py` calls it first.
- [x] Non-admin users shouldn't even see these nav items/settings panels
  (hide, don't just 403). Both `#github-intg-card` and `#discord-intg-card`
  now carry the existing `admin-only` class (`static/index.html`) that
  `syncAdminVisibility()` already applies to every other admin-gated element
  each time the Settings modal opens (`settings.js`) — no new mechanism
  needed, just using the one already there.
- [ ] Scoped API tokens (the `/api/codex/*` scope system used by Claude/Codex
  companions, §0) are a *separate* concern from this — those are for external
  agents acting on behalf of the admin, still ultimately admin-owned.
- [ ] Extend `THREAT_MODEL.md` / `SECURITY.md` with the new credential classes
  (GitHub OAuth token, Discord bot token) — not done yet, flagged as a
  follow-up in §1/§2 above. VRChat session still N/A (not built).

---

## Decisions locked in (was "open questions")

1. **Security posture**: admin-only for all of §1/§2/§5 — see §7 above.
2. **Build order**: **GitHub repo workflow (§2) first**, as the primary
   developer-facing feature. Discord (§1), VRChat (§5), and the rest follow after.
3. **GitHub auth**: **OAuth App**, specifically *because* it needs to create new
   repos (not just push to existing ones) and upload/init local projects into
   them — see the expanded §2 above for the create-repo + upload-existing-project flow.
4. **Image generation**: support **every** path — OpenAI-compatible *and*
   self-hosted (A1111/ComfyUI/SD.Next) — as parallel, independently configurable
   backends, not a single either/or choice.
