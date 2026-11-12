FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RELAY_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY crm ./crm
COPY templates ./templates
COPY static ./static
COPY run.py ./
RUN useradd --system --uid 10001 relay && mkdir -p /data && chown relay /data
USER relay
VOLUME ["/data"]
EXPOSE 5055
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5055/healthz')"
CMD ["python", "run.py", "--service", "--host", "0.0.0.0", "--port", "5055"]
