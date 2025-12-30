FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

RUN useradd --system --create-home --home-dir /app collector && mkdir -p /data && chown collector:collector /data

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY evohome_logger.py ./

USER collector
VOLUME ["/data"]

ENTRYPOINT ["python", "/app/evohome_logger.py"]
