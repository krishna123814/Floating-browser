#!/bin/sh
set -e

# Nginx apne default daemon mode mein background me chala jaata hai
nginx

# WebDAV (/data) — 127.0.0.1:8504, nginx /dav ke peeche. Fail ho to app na ruke.
python dav_server.py &

# Streamlit foreground me — container ka main (PID 1) process.
# Andar port 8000 par, bahar se seedha reachable nahi (nginx ke peeche).
exec streamlit run app.py \
    --server.port=8000 \
    --server.address=127.0.0.1 \
    --server.headless=true \
    --browser.gatherUsageStats=false
