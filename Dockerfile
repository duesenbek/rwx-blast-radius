FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
RUN pip install --no-cache-dir --disable-pip-version-check pytz==2026.6
COPY dependencies.json.gz /app/dependencies.json.gz
COPY selector.py /app/selector.py
COPY runtime_probe.py baseline.zip /app/
COPY prepare_runtime.py /app/prepare_runtime.py
RUN python /app/prepare_runtime.py
CMD ["python", "/app/selector.py"]
