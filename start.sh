#!/bin/bash
# One-command start for the AI Engineering Copilot.
# The ENTIRE app runs inside Docker Desktop — if Docker is not running,
# every page is blank. This script makes sure Docker is up, then starts
# (or reuses) the stack.
set -e
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop is not running — starting it (this can take ~30s)..."
  open -a Docker
  until docker info >/dev/null 2>&1; do printf "."; sleep 2; done
  echo " up."
fi

docker compose up -d

echo
echo "  App:      http://localhost:5173"
echo "  API docs: http://localhost:8000/docs"
echo
echo "  Follow ingestion progress:  docker compose logs -f worker"
echo "  Stop everything:            docker compose down   (data is kept)"
