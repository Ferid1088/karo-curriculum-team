FROM python:3.12-slim

# Node + claude CLI werden für den Provider "claude_token" (Claude-Abo) gebraucht.
ARG INSTALL_CLAUDE_CLI=true
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && if [ "$INSTALL_CLAUDE_CLI" = "true" ]; then \
         curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
         && apt-get install -y --no-install-recommends nodejs \
         && npm install -g @anthropic-ai/claude-code ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# nicht als root laufen (die claude CLI verweigert manche Modi als root)
RUN useradd -m kcteam && mkdir -p /data /karo /karo-export && chown -R kcteam /app /data /karo-export
USER kcteam

# Ohne das puffert Python seine Ausgabe, sobald sie in eine Pipe geht: in
# `docker logs` erscheint dann waehrend der Arbeit gar nichts, und erst ein
# Absturz spuelt den Puffer. Ein Agent, der minutenlang schweigt, ist im
# Betrieb nicht nachvollziehbar.
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "kcteam"]
CMD ["run"]
