FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grr ./grr
COPY run.py ./

ENV DATABASE=/data/grr.db \
    DEMO_SEED=1 \
    PORT=8000
RUN mkdir -p /data
EXPOSE 8000

CMD ["python", "run.py"]
