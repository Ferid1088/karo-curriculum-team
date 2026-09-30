FROM python:3.12-slim

# Node + claude CLI werden für den Provider "claude_token" (Claude-Abo) gebraucht.
ARG INSTALL_CLAUDE_CLI=true
# git: karo-contract kommt aus Karos Repository (siehe requirements.txt).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git \
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

# Woraus dieses Image gebaut wurde. Ohne diese Angabe laesst sich nicht
# feststellen, ob ein laufender Dienst einem Stand entspricht, den es im
# Repository ueberhaupt gibt — genau daran ist ein Ende-zu-Ende-Lauf schon
# einmal gescheitert: das Image trug Code aus einem schmutzigen
# Arbeitsbaum. `make build` reicht den Wert ein und weigert sich, wenn
# etwas nicht committet ist.
ARG KCTEAM_GIT_SHA=unbekannt
ENV KCTEAM_GIT_SHA=$KCTEAM_GIT_SHA
LABEL org.opencontainers.image.revision=$KCTEAM_GIT_SHA
ENTRYPOINT ["python", "-m", "kcteam"]
CMD ["run"]
