#!/bin/sh
# Baut die Images — aber nur aus einem sauberen, committeten Stand.
#
# Ein Image aus einem schmutzigen Arbeitsbaum laeuft mit Code, den kein
# Repository kennt. Was darauf geprueft wird, kann niemand wiederholen.
set -eu
cd "$(dirname "$0")"

if [ -n "$(git status --porcelain)" ]; then
  echo "ABBRUCH: Der Arbeitsbaum ist nicht sauber." >&2
  echo "Ein Image aus nicht committetem Code ist nicht reproduzierbar." >&2
  echo >&2
  git status --short >&2
  exit 2
fi

SHA=$(git rev-parse HEAD)

# Und nur aus einem Stand, den es auch anderswo gibt. Ein Image aus einem
# Commit, der nur auf diesem Rechner liegt, laesst sich nicht nachvollziehen:
# die Zeile im Image zeigt auf nichts.
if ! git branch -r --contains "$SHA" | grep -q .; then
  echo "ABBRUCH: $SHA ist auf keinem Remote." >&2
  echo "Erst pushen, dann bauen — sonst zeigt die SHA im Image ins Leere." >&2
  exit 3
fi

echo "Baue aus $SHA"
# Alle Profile: die Dienste liegen in Profilen ("agent", "admin", "cli"), und
# ohne Profil baute `docker compose build` still gar nichts — es meldete
# "No services to build" und gab 0 zurueck. Ein Build, der nichts baut und
# trotzdem "Fertig" sagt, ist schlimmer als keiner.
docker compose --profile "*" build --build-arg "KCTEAM_GIT_SHA=$SHA" "$@"
echo "Fertig. Image-Stand: $SHA"
