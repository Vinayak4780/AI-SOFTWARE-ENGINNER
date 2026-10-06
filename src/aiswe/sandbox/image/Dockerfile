# Base sandbox image for the AI software engineer.
# One ephemeral container of this image is started per task (see src/aiswe/sandbox/docker.py).
# Pinned by digest so a rebuild can't silently pull a different base image.
FROM python:3.11-slim@sha256:a2bc8c35469b6fe37735f7c4dae39049470b2ce068e73f799c02452de31d24c6

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (gh), for tools/github.py's git_push/create_pull_request/get_issue/
# comment_issue -- official install method per cli.github.com, not a guessed
# binary URL/version, since apt doesn't carry it directly.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Security scanners for security/scanners.py. gitleaks: pinned release,
# verified against its published SHA-256 for this architecture.
ARG GITLEAKS_VERSION=8.30.1
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) asset=linux_x64;   sum=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;; \
      arm64) asset=linux_arm64; sum=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/gitleaks.tgz "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_${asset}.tar.gz"; \
    echo "${sum}  /tmp/gitleaks.tgz" | sha256sum -c -; \
    tar -xzf /tmp/gitleaks.tgz -C /usr/local/bin gitleaks; \
    rm /tmp/gitleaks.tgz

RUN pip install --no-cache-dir pytest bandit==1.9.4 semgrep==1.179.0

RUN useradd -m -u 1000 sandboxuser

COPY helper.py /usr/local/bin/aiswe_helper.py
COPY semgrep-rules.yml /opt/aiswe/semgrep-rules.yml
RUN chmod 755 /usr/local/bin/aiswe_helper.py && chmod 644 /opt/aiswe/semgrep-rules.yml

# The container runs with a read-only root filesystem and a fresh tmpfs home
# (sandbox/docker.py); git's safe.directory is set per container in prepare_git().
USER sandboxuser
WORKDIR /workspace
