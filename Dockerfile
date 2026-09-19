FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=5000 \
    MIDWARE_DB=/data/midware.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY midware ./midware
COPY wsgi.py run.py ./

# Persist the SQLite database (and its WAL sidecars) outside the image.
VOLUME ["/data"]
EXPOSE 5000

CMD ["python", "wsgi.py"]
