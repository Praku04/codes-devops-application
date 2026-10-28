#!/bin/bash
# Start server with no upload timeout — required for large video files
# Usage:
#   ./start_server.sh          → production (EC2)
#   ./start_server.sh --reload → local dev with auto-reload

exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --timeout-keep-alive 3600 \
    --h11-max-incomplete-event-size 0 \
    --workers 1 \
    "$@"
