# IMV en conteneur, pour un serveur en ligne (voir deploy/README.md).
# Image Playwright : Chromium et ses dépendances système, à la même version que le paquet Python.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml NOTICE ./
COPY imo ./imo
COPY imo_agent.py ./
# Python système de l'image (sans venv) : le conteneur ne sert qu'à IMV.
RUN python3 -m pip install --no-cache-dir --break-system-packages "playwright==1.64.1" . \
    && install -d -o pwuser -g pwuser /data

# Claude Code (fournisseur LLM claude-cli) : binaire natif, version figée, sans mise à jour
# automatique ; connexion par CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), voir deploy/README.md.
ARG CLAUDE_CODE_VERSION=2.1.311
ENV PATH=/home/pwuser/.local/bin:$PATH \
    DISABLE_AUTOUPDATER=1
USER pwuser
RUN curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" \
    && claude --version

# Tout ce qui est propre à l'instance est dans /data (volume) : base sessions/imv.db, captures debug/,
# contexte.md, et .env éventuel, lus depuis le répertoire courant comme en local.
WORKDIR /data
EXPOSE 8765
ENTRYPOINT ["python", "/app/imo_agent.py"]
CMD ["serve", "--auth", "--host", "0.0.0.0"]
