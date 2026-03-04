# gunicorn.conf.py — Production WSGI config
import os
import multiprocessing

# Workers: (2 × CPU cores) + 1
workers = int(os.getenv("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))

# Gevent workers for streaming (video range requests)
# worker_class = "gevent"

# Timeouts
timeout = 300        # 5 min for large video uploads
keepalive = 5

# Bind
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:5000")

# Logging
accesslog = "-"
errorlog  = "-"
loglevel  = os.getenv("GUNICORN_LOG_LEVEL", "info")

# Limits
# No limit on request body size (video uploads)
limit_request_line = 0
limit_request_fields = 200
