#!/usr/bin/env bash
# Build-time install of Claude Code into a system location.
#
# Consumers invoke this as the last step of their docker-setup/Dockerfile,
# so a version bump rebuilds nothing but this layer:
#     ARG CLAUDE_CODE_VERSION=latest
#     COPY install-claude.sh /tmp/
#     RUN bash /tmp/install-claude.sh "$CLAUDE_CODE_VERSION"
#
# The ARG is what makes updates happen: build_image() resolves the configured
# channel to a concrete version and passes it as a build arg, so the RUN layer
# is invalidated exactly when a new release is out. Without it, the RUN line
# never changes and Docker keeps serving the first cached install.
#
# It runs at *build* time, before entrypoint.sh creates the dev user, so the
# install must land somewhere on every user's PATH rather than in a per-user
# ~/.local. We use the native installer (no node/npm dependency) and relocate
# the resulting binary to /usr/local/bin/claude.
#
# Credentials/login are NOT handled here: the launcher bind-mounts the host's
# ~/.claude into the container (see docker_ops._convenience_mounts), so the
# OAuth token in ~/.claude/.credentials.json carries over automatically.
set -euo pipefail

# Pin via the first arg or CLAUDE_CODE_VERSION (e.g. "1.2.3"); default latest.
VERSION="${1:-${CLAUDE_CODE_VERSION:-latest}}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "install-claude.sh: missing required command '$1'" >&2; exit 1; }; }
need curl

echo "install-claude.sh: installing Claude Code ($VERSION) via native installer..."
curl -fsSL https://claude.ai/install.sh | bash -s -- "$VERSION"

# The native installer drops a launcher into the invoking user's ~/.local/bin
# (root, during build). Resolve it and relocate to a system path so the
# runtime dev user finds it on PATH.
src=""
for cand in "$HOME/.local/bin/claude" "/root/.local/bin/claude"; do
  if [ -x "$cand" ]; then src="$cand"; break; fi
done
if [ -z "$src" ]; then
  src="$(command -v claude || true)"
fi
if [ -z "$src" ] || [ ! -e "$src" ]; then
  echo "install-claude.sh: could not locate the installed 'claude' launcher" >&2
  exit 1
fi

# Copy the launcher (and its versioned payload, if the launcher is a symlink
# into ~/.local/share) into /usr/local so it survives outside the build user's
# home. We resolve the real target and install both the real file and a
# /usr/local/bin/claude pointing at it.
real="$(readlink -f "$src")"
install -d /usr/local/share/claude
cp -a "$real" /usr/local/share/claude/claude
ln -snf /usr/local/share/claude/claude /usr/local/bin/claude
chmod 0755 /usr/local/bin/claude /usr/local/share/claude/claude

echo "install-claude.sh: installed -> $(/usr/local/bin/claude --version 2>/dev/null || echo '/usr/local/bin/claude')"
