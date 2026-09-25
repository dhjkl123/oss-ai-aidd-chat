# The Image for running Cite under local Docker. Everything that differs between
# machines (endpoint, key, Wiki, tokenizer) comes in through the environment and
# Volumes at `docker run` time, never through this file.
#
# Three versions are pinned in this repo and all three are asserted below, because a
# Base Image that drifts from them builds an app nobody tested:
#   pyproject.toml  requires-python = "==3.12.14"
#   .python-version 3.12.14
#   [tool.uv]       required-version = "==0.12.5"
#
# Both bases are pinned BY DIGEST, not by tag: a tag can be re-pointed at a new
# build, and the version asserts below would then pass against an image nobody
# reviewed. Build with `--pull` to make the digest the thing that is fetched.
#
# The pi sidecar's own stage, built first: `npm ci` needs Node before this repo's
# Python base has it, and building it separately keeps the sidecar's node_modules
# out of the final Image's layers -- only the pruned /agent tree and the node
# binary itself cross over, via the COPY --from below.
FROM node:22.19.0-bookworm-slim@sha256:4a4884e8a44826194dff92ba316264f392056cbe243dcc9fd3551e71cea02b90 AS agent
WORKDIR /agent
COPY agent/package.json agent/package-lock.json ./
RUN npm ci --omit=dev
COPY agent/*.mjs ./

FROM python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579

# UID 1000 first, before anything is written into /app: creating the user afterwards
# and running `chown -R` over the venv writes a second full copy of it into a new
# layer. `COPY --chown` and an already-owned WORKDIR cost nothing.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

# uv's own `required-version` gate refuses a mismatched uv, so copying the pinned
# binary in is what makes `uv sync --locked` below mean anything.
COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /uvx /usr/local/bin/
RUN python -c "import sys; assert sys.version.split()[0] == '3.12.14', sys.version" \
    && uv --version | grep -q "^uv 0.12.5"

# Byte-compile on install and never write .pyc at runtime: the runtime user owns
# nothing outside /app, and a read-only or foreign-owned tree is not somewhere to
# discover that at the first request.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
RUN chown 1000:1000 /app
USER 1000

# THIS is the credential guarantee: an explicit allowlist of what enters the Image.
# `.dockerignore` is a second line -- it shrinks the context and keeps a stray secret
# out of the daemon -- but a file that no COPY names cannot reach a layer whatever
# the ignore file says. Four paths, and every one of them is committed.
#
# The Lockfile layer is separate so a source edit does not re-resolve the
# environment. `--locked` FAILS on a Lockfile that disagrees with pyproject.toml
# rather than quietly re-locking, which is the whole point of building from one.
# `--no-dev` keeps the dev group -- playwright above all -- out of the Image.
COPY --chown=1000:1000 pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY --chown=1000:1000 src ./src
RUN uv sync --locked --no-dev

# The sidecar the wiki agent spawns as a child process (AGENT_SCRIPT resolves to
# /app/agent/sidecar.mjs). Node itself has to come along too -- the final stage's
# base is the Python Image, which has no node on PATH.
COPY --from=agent /usr/local/bin/node /usr/local/bin/node
COPY --from=agent --chown=1000:1000 /agent /app/agent

# The app listens here; `docker run -p <host>:7860` publishes it.
EXPOSE 7860

# One worker, one process, one replica -- every ceiling this app enforces is
# in-memory, so a second worker silently halves all of them. Not a shell form:
# PID 1 must be uvicorn itself so a platform's SIGTERM reaches it and lifespan
# shutdown runs.
#
# `--proxy-headers` is uvicorn's default, spelled out because this app DEPENDS on it:
# behind a TLS-terminating router the request scheme is https and the Origin gate
# compares scheme://netloc exactly, so without X-Forwarded-Proto every mutating POST
# answers 403. It only takes effect for peers named by FORWARDED_ALLOW_IPS, which is
# unset here on purpose -- a real deployment behind such a router sets it (and
# TRUSTED_PROXY_HOPS) for its own router.
CMD ["python", "-m", "uvicorn", "aidd_chat.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1", "--proxy-headers"]
