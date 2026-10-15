# ZAI serverless gateway. Small on purpose: tornado and PyYAML, nothing else.
FROM python:3.12-slim
RUN pip install --no-cache-dir tornado==6.5.8 PyYAML==6.0.3 \
 && useradd --uid 10001 --create-home gateway
COPY gateway.py /app/gateway.py
USER 10001
WORKDIR /app
ENTRYPOINT ["python", "/app/gateway.py"]
