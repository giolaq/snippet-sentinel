FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends docker.io git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
RUN mkdir /data
EXPOSE 8000
CMD ["snippet-sentinel", "serve", "--host", "0.0.0.0", "--db", "/data/sentinel.db"]
