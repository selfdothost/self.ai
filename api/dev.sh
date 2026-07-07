PORT="${PORT:-8080}"
uvicorn selfai_ui.main:app --port $PORT --host 0.0.0.0 --forwarded-allow-ips '*' --reload