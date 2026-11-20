#!/usr/bin/env bash
set -euo pipefail

LABEL=$1
WORKERS=$2
export QUEUE_MAX_LENGTH=$3
export PREFETCH_COUNT=$4
SCALE_AT=${5:-0}
DURATION=${DURATION:-120}

export RUN_LABEL=$LABEL
export SOURCE_DURATION_SEC=$DURATION
if [ "$QUEUE_MAX_LENGTH" = "0" ]; then
  export TASKS_QUEUE=flight.tasks.unbounded
else
  export TASKS_QUEUE=flight.tasks
fi

echo ">>> $LABEL: workers=$WORKERS queue=$TASKS_QUEUE max_length=$QUEUE_MAX_LENGTH prefetch=$PREFETCH_COUNT duration=${DURATION}s"
rm -rf "experiments/results/$LABEL"
docker compose exec -T db psql -U postgres -d flight_delay -q \
  -c "DELETE FROM tasks WHERE source = '$LABEL'" >/dev/null 2>&1 || true
mkdir -p "experiments/results/$LABEL"

docker compose up -d --scale worker="$WORKERS" worker
echo ">>> waiting for workers to become healthy..."
until [ "$(docker compose ps worker --format '{{.Health}}' | grep -c healthy)" -ge "$WORKERS" ]; do
  sleep 2
done

uv run python experiments/monitor.py --label "$LABEL" --queue "$TASKS_QUEUE" \
  --duration $((DURATION + 30)) &
MONITOR_PID=$!

if [ "$SCALE_AT" -gt 0 ]; then
  (sleep "$SCALE_AT" && echo ">>> scaling to 2 workers" \
    && docker compose up -d --scale worker=2 --no-recreate worker) &
fi

docker compose --profile load run --rm source

echo ">>> source finished, observing for 30s more"
wait "$MONITOR_PID"

docker compose stop -t 2 worker >/dev/null
docker compose exec -T rabbitmq rabbitmqctl purge_queue "$TASKS_QUEUE" >/dev/null || true
echo ">>> done: experiments/results/$LABEL"
