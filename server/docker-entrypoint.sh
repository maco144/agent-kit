#!/bin/sh
# Apply migrations (API container only), then run the given command.
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "agent-kit Cloud: applying database migrations"
  alembic upgrade head
fi

exec "$@"
