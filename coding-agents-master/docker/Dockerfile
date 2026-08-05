FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest
ARG INCLUDE_LIBREOFFICE=true

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        iptables ipset dnsutils ca-certificates procps \
    && if [ "$INCLUDE_LIBREOFFICE" = "true" ]; then \
        apt-get install -y --no-install-recommends libreoffice-calc; fi \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages --no-cache-dir openpyxl pandas

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
                   "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

RUN useradd -m -u 1001 -s /bin/bash agent

COPY init-firewall.sh /usr/local/bin/init-firewall.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/init-firewall.sh /usr/local/bin/entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
