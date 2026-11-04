# Local Development Setup & Deployment

Instructions for building, running, and deploying the service locally or in staging environments.

---

## Environment Setup Steps

```bash
# 1. Clone repository
git clone <repository-url>
cd calendar-scheduling-api

# 2. Initialize virtual environment
python -m venv env
source env/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Spin up local Redis & PostgreSQL containers
docker compose up -d postgres redis

# 5. Run database migrations
alembic upgrade head

# 6. Start FastAPI service
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Production Docker Multi-Stage Build

The project includes a production-ready `Dockerfile`:

```bash
docker build -t calendar-scheduling-api:latest .
docker run -p 8000:8000 --env-file .env calendar-scheduling-api:latest
```
