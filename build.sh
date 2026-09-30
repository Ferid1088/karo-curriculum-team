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
echo "Baue aus $SHA"
docker compose build --build-arg "KCTEAM_GIT_SHA=$SHA" "$@"
echo "Fertig. Image-Stand: $SHA"
