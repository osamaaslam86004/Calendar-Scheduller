# Scaling Across Multiple Celery Workers Doing Different Tasks
Yes, the module-level lazy caching approach (`_worker_engine`) is designed to scale across multiple Celery worker processes and heterogeneous task types.

## How it operates across workers:
Process Isolation: Celery workers run as separate operating system processes. Each worker process initializes its own `_worker_engine` once when executing its first task, building its own connection pool according to `settings.db.pool_size`.  

## Multi-Task Safety: 
If Worker A executes `execute_calendar_schedule_task` and then executes a `send_email_notifications_task`, both tasks safely reuse the same worker process's `_worker_engine` pool without overhead.Pool Capacity 

## Safeguard: 
If you scale to 10 Celery worker processes with `pool_size=20`, your database will receive up to 200 concurrent connections ($10 \times 20$). Be sure to set `pool_size` in `config/settings.py` to a reasonable limit relative to your PostgreSQL `max_connections` allowance