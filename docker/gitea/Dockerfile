# Image for the devenv-gitea service container: the machine-wide Gitea
# instance behind the PR-review workflow (see GITEA.md). Built locally by
# gitea_service.py / the setup wizard; all state lives in a bind-mounted host
# directory, so the container itself is disposable.
FROM debian:bookworm-slim

ARG GITEA_VERSION=1.26.4

# git: required by Gitea for all server-side repo operations.
# nginx: fronts Gitea and stamps the reverse-proxy auth header (no-login UX).
# gosu: drop from root to the host user's UID after entrypoint setup.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git nginx gosu curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://dl.gitea.com/gitea/${GITEA_VERSION}/gitea-${GITEA_VERSION}-linux-amd64" \
      -o /usr/local/bin/gitea && \
    chmod +x /usr/local/bin/gitea

COPY gitea-entrypoint.sh /usr/local/bin/gitea-entrypoint.sh
RUN chmod +x /usr/local/bin/gitea-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/gitea-entrypoint.sh"]
