# Deploying Hermes (claude-code provider) on Coolify

This deploys a **single self-contained WebUI container** with this fork's agent
(the `claude-code` subscription provider) and the native `claude` CLI baked in,
and sets up **auto-redeploy on push to `main`**.

- Image: [`docker/webui.coolify.Dockerfile`](docker/webui.coolify.Dockerfile)
- Compose: [`docker-compose.coolify.yml`](docker-compose.coolify.yml)
- Seed wrapper: [`docker/webui-coolify-entrypoint.sh`](docker/webui-coolify-entrypoint.sh)

## How it works (why this survives redeploys)

- Your agent source is **baked into the image at `/opt/hermes`** (not a volume),
  so every build ships fresh code — a push to `main` actually deploys new code.
- Only user state lives on the persistent **`hermes-home`** volume: `config.yaml`,
  the `claude login` session (`CLAUDE_CONFIG_DIR`), and WebUI sessions. Redeploys
  keep it, so **you log in once** and it survives every future deploy.
- On a fresh volume the entrypoint seeds `config.yaml` with `claude-code` as the
  **default provider**, so it works out of the box and shows in the model picker.

The WebUI runs the agent in-process, so no separate gateway container is needed.

---

## One-time setup in Coolify

### 1. Connect the repo (enables auto-deploy on push)

1. Coolify → **Sources** → **GitHub** → **Connect** (install the Coolify GitHub
   App on `omarkad2/hermes-agent`). This auto-creates the push webhook — no
   manual webhook needed.
2. (Alternative if you skip the App: Coolify gives the resource a webhook URL in
   step 2; add it to GitHub → repo **Settings → Webhooks**, content-type
   `application/json`, event = *push*.)

### 2. Create the resource

1. Coolify → your project → **+ New** → **Docker Compose** (Application from a
   Git repository).
2. Repository: `omarkad2/hermes-agent` · Branch: **`main`** · Build path: `/`.
3. **Docker Compose Location:** `docker-compose.coolify.yml`.
4. Enable **Auto Deploy** (deploy on push to the chosen branch).

### 3. Environment variables (resource → Environment)

| Variable | Value | Notes |
|---|---|---|
| `HERMES_WEBUI_PASSWORD` | *a strong password* | **Required.** This is your WebUI login; the public URL is exposed. |
| `GH_TOKEN` | *a GitHub PAT* | Optional — lets the agent clone **private** repos. See below. |
| `HERMES_WEBUI_TAG` | `0.51.92` | Optional — pin the base WebUI image version. |

### Cloning private GitHub repos

The image bundles `gh` and configures git to use it as the credential helper for
`github.com`, both reading the `GH_TOKEN` env var — nothing is written to the
container filesystem, so it survives redeploys (a container-side `gh auth login`
would NOT, since `~/.config/gh` and `~/.gitconfig` are outside the persistent
volume).

1. Create a GitHub **Personal Access Token** with `repo` scope (classic) or a
   fine-grained token with read access to the repos you want.
2. Set `GH_TOKEN` to that token in the Coolify resource's **Environment**.
3. Redeploy. The agent can now `git clone https://github.com/owner/repo.git`
   (private included) and run `gh` commands — no interactive login.

Leave `GH_TOKEN` unset for public-only repos.

### 4. Persistent storage

The compose declares named volumes `hermes-home` and `hermes-workspace`; Coolify
persists them across deploys automatically. **Do not** set them to ephemeral —
`hermes-home` holds your `claude login` and config.

### 5. Domain / port

Assign a domain to the `hermes-webui` service pointing at container port **8787**
(Coolify's proxy/Traefik handles TLS). The container only `expose`s 8787; Coolify
maps your domain to it.

### 6. Deploy

Click **Deploy**. The **first** boot is slow — it runs `uv pip install
hermes-agent[all]` inside the container (several minutes; the healthcheck
`start_period` allows for it). Subsequent boots are fast.

### 7. Log in to Claude (one time)

The provider needs a logged-in `claude` session. After the first deploy:

1. Coolify → resource → **Terminal** (exec into the running container), then:
   ```sh
   CLAUDE_CONFIG_DIR=/home/hermeswebui/.hermes/claude \
     /usr/local/bin/claude login
   ```
2. Follow the URL/code prompt (open the URL in any browser, authorize, paste the
   code back). This writes credentials to the persistent volume.

That's it — open your domain, log in with `HERMES_WEBUI_PASSWORD`, and **Claude
Code → sonnet** is already the default model. It bills your Claude subscription.

---

## Auto-deploy on push to `main`

With the GitHub App connected and **Auto Deploy** on, every push to `main`
triggers a rebuild + redeploy. Because the agent code is baked into the image,
each deploy ships the pushed code; because `hermes-home` persists, your login and
config carry over untouched. No re-login needed after a deploy.

## Maintenance notes

- **Updating the `claude` CLI:** it's installed fresh (`claude.ai/install.sh`) on
  every image build, so any redeploy picks up the latest. To force it, redeploy
  with "no cache".
- **Security:** the WebUI is internet-exposed behind a password only — use a
  strong `HERMES_WEBUI_PASSWORD`, and consider Coolify access controls / putting
  it behind your own auth if needed. The `claude login` session lives on the
  server volume.
- **Tradeoffs of the provider** (no token streaming, no tool use in v1,
  per-request process spawn, subscription rate limits) are documented in
  `CHANGES.md`.
- **Resetting:** to wipe the login/config and start fresh, delete the
  `hermes-home` volume in Coolify and redeploy, then `claude login` again.
