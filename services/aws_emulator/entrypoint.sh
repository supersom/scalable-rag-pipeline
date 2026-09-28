#!/bin/sh
set -eu

moto_server -H 0.0.0.0 -p 4566 &
server_pid=$!
trap 'kill "${server_pid}" 2>/dev/null || true' INT TERM EXIT

python -m services.aws_emulator.init_s3
python -m services.aws_emulator.init_eval_queue
wait "${server_pid}"
