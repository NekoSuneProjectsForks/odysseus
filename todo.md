# Odysseus — Feature TODO / Ideas

Working notes from a codebase pass, answering "what's already here" and laying
out ideas for: Discord, GitHub/coding-agent workflows, image gen, an in-app
coding assistant, VRChat/VRCX, and general gaming/content/dev extras.

Nothing below is committed to — it's a menu to prioritize from.

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

## 1. Discord Bot Integration

Needs a real bot (gateway connection + bot token), not just the existing outgoing
webhook. Two viable shapes:

- [ ] **Decide bot architecture**: run a `discord.py` (or `nextcord`/`hikari`)
  process alongside the FastAPI app (own service under `services/discord/`,
  similar shape to `services/stt`, `services/tts`), talking to Odysseus over an
  internal API/queue — vs. embedding an async client directly in `app.py`'s
  event loop. Given the existing `services/*` service pattern, a standalone
  `services/discord/service.py` + bot process (started/stopped like STT/TTS) fits
  best.
- [ ] New Settings > Integrations > "Discord Bot" panel: paste bot token (encrypted
  via `src/secret_storage.py`, same pattern as other integrations), pick guild(s),
  enable/disable, required intents checklist (message content + presence are
  *privileged intents* — must be enabled in the Discord Developer Portal per-bot,
  document this clearly in-app since it's the #1 setup failure mode).
- [ ] Read messages / message history from a channel (needs `MESSAGE_CONTENT` intent).
- [ ] Search message history (by keyword/author/date) — likely backed by a local
  cache table (own message archive) rather than re-querying Discord's REST API
  every time, since Discord's history endpoint is paginated/rate-limited.
- [ ] Guild member count + online/idle/dnd/offline presence roster (needs
  `GUILD_PRESENCES` intent + `GUILD_MEMBERS` intent).
- [ ] Send messages / replies from Odysseus (agent-triggerable tool + manual UI).
- [ ] Expose as agent tools (`send_discord_message`, `search_discord_history`,
  `get_guild_members`) so chat/agent sessions can act on Discord data, gated by
  the same per-agent-token scope system used for `/api/codex/*` (§0).
- [ ] Rate-limit / backoff handling for Discord API (429s) — don't hand-roll,
  reuse whatever discord.py library gives for free.
- [ ] Threat-model note: bot token is as sensitive as an admin password — extend
  `THREAT_MODEL.md` and `SECURITY.md` once this lands.

## 2. GitHub Integration — clone / edit / push / PR ("like Claude/Codex do it")

**STATUS: primary/first feature to build.** This is the big one — a "coding
agent operating on a real repo" workflow inside Odysseus, distinct from the
existing external Claude/Codex companion bundles (§0). **Admin-only, full stop**
(see §7) — no other account role gets any access to this surface.

- [x] **Auth — decided: GitHub OAuth App**, not PAT. Needs:
  - [ ] Register a GitHub OAuth App (client id/secret in admin settings, same
    encrypted-secret pattern as everything else via `src/secret_storage.py`).
  - [ ] OAuth callback route (`routes/` — new `github_routes.py`), authorize
    with scopes covering `repo` (private repo read/write) at minimum; consider
    whether `workflow` scope is needed later for editing Actions files.
  - [ ] Token refresh — GitHub OAuth App user tokens don't expire by default
    unless the app opts into expiring tokens; decide which mode and handle
    refresh if enabled.
- [x] **Repo creation — decided: must support creating a new repo, not just
  cloning existing ones.**
  - [ ] "Create repo" flow via `POST /user/repos` (or `/orgs/{org}/repos` for
    org-owned repos) — name, description, private/public, optional
    license/gitignore/README init.
  - [ ] After creation, immediately clone it locally so it drops into the same
    workspace flow as an existing-repo clone (below) — one unified path either way.
  - [ ] "Upload there" — support pushing an existing local project (e.g. something
    the user already has, or a repo-less scratch project the agent built) up to
    a newly created repo: `git init` (if needed) → add remote → first commit → push.
- [ ] **Repo workspace model**: a `data/repos/<project>/` working-copy area (own
  DB table: project name, remote URL, local path, default branch, owning admin,
  created-vs-cloned flag). Clone/init via the existing shell service
  (`services/shell`) running real `git`, not a reimplementation.
- [ ] **Project switcher UI**: list of repos (created + cloned), "New repo" and
  "Clone existing repo" (owner/repo or full URL) actions, branch picker, "open
  in agent" action that scopes the chat/agent session's shell + file tools to
  that repo's working directory.
- [ ] **Agent coding loop**: reuse `src/agent_loop.py` + shell/file tools, scoped to
  the repo path, so an Odysseus chat session can read files, edit, run
  tests/build, and iterate — this is the "acts like Claude Code/Codex" ask.
  Consider a dedicated system prompt/mode (like Agent mode already has) tuned
  for "coding agent in a repo" vs. general chat.
- [ ] **Diff review UI** before committing — show working-tree diff, let the user
  approve/reject hunks (don't auto-commit silently; this is the #1 trust issue
  with in-app coding agents).
- [ ] **Commit + push** flow with commit message (agent-drafted, user-editable).
- [ ] **Pull request creation** via GitHub REST/GraphQL API (needs `repo` scope):
  create branch, push, open PR with title/body, link back in UI.
- [ ] **PR status / CI checks** surface (poll GitHub Checks API) so the user sees
  pass/fail without leaving Odysseus.
- [ ] Decide: is this its own top-level nav item ("Projects"/"Code") or a mode
  within the existing Agent/Chat surface? Given Odysseus's existing pattern
  (Chat, Cookbook, Deep Research, Compare, Documents, Email, Notes/Tasks/Calendar
  as distinct top-level sections — see README §Features), a new **"Projects"**
  section seems most consistent.

## 3. Image Generation — round out what's already there

**Decided: support every generation path** — OpenAI/OpenAI-compatible APIs
*and* self-hosted backends, side by side (not either/or). `mcp_servers/image_gen_server.py`
already covers OpenAI-compatible APIs (OpenAI itself, and any local server that
speaks the same `/images/generations` shape, e.g. LocalAI). Remaining:

- [ ] **Self-hosted backend adapters**: Stable Diffusion WebUI (A1111) API, ComfyUI
  API, and SD.Next as additional backends alongside the OpenAI-compatible path —
  a `image_backend: openai | automatic1111 | comfyui | sdnext` setting (multi-backend
  registration, not a single global toggle, so a user can configure several and
  pick per-generation), each with its own base-URL + request-shape adapter.
  Check `services/hwfit/image_models.py` (Cookbook's local image model metadata)
  before building a parallel model-serving concept — reuse if it already scopes this.
- [ ] Image-to-image / inpainting support (gallery already has an editor per
  README's "gallery/image editor" feature — confirm current scope, extend if
  it's generation-only today).
- [ ] Model/LoRA picker for self-hosted backends (A1111/ComfyUI expose these via API).
- [ ] Cost/quota guardrails for paid API backends (soft limit + admin setting),
  since image gen is the highest-cost-per-call agent tool by far.

## 4. In-app Coding Assistant ("Codex/Claude, but built in")

Overlaps heavily with §2 but is broader — coding help isn't only "operate on a
cloned GitHub repo," it's also ad hoc code write/explain/debug in chat.

- [ ] Confirm current Agent mode's code-handling today (file tools, shell tool,
  syntax highlighting in Documents editor) — likely already decent for
  single-file/snippet work; the gap is specifically the *multi-file, real-repo,
  git-aware* workflow in §2.
- [ ] Sandboxing question: local shell exec is real and unsandboxed (admin-only
  today). Decide whether repo-scoped coding-agent execution needs stronger
  isolation (container-per-project, `docker/` already has patterns for this) so
  a coding agent working in one project can't touch another project's data or
  the host outside `data/repos/<project>/`.
- [ ] Model routing: coding tasks benefit from designating a specific model/preset
  (existing `preset_manager.py`) as "coding model" separate from general chat model.

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

- [ ] Every new route for these features goes behind the same admin gate used
  by `routes/shell_routes.py` (`_require_admin`), not just "logged in."
- [ ] Non-admin users shouldn't even see these nav items/settings panels
  (hide, don't just 403) once multi-user/roles matter here.
- [ ] Scoped API tokens (the `/api/codex/*` scope system used by Claude/Codex
  companions, §0) are a *separate* concern from this — those are for external
  agents acting on behalf of the admin, still ultimately admin-owned.
- [ ] Extend `THREAT_MODEL.md` / `SECURITY.md` once any of these land, given
  the new credential classes involved (GitHub OAuth token, Discord bot token,
  VRChat session).

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
