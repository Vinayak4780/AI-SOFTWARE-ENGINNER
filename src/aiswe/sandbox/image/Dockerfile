# Base sandbox image for the AI software engineer.
# One ephemeral container of this image is started per task (see src/aiswe/sandbox/docker.py).
FROM python:3.11-slim

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

RUN useradd -m -u 1000 sandboxuser

COPY helper.py /usr/local/bin/aiswe_helper.py
RUN chmod 755 /usr/local/bin/aiswe_helper.py \
    && pip install --no-cache-dir pytest

USER sandboxuser
RUN git config --global --add safe.directory /workspace
WORKDIR /workspace
