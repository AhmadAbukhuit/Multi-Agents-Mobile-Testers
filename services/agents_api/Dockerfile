FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    android-tools-adb \
    curl \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt /workspace/requirements.txt
RUN pip install --no-cache-dir -r /workspace/requirements.txt

COPY . /workspace
RUN mkdir -p /workspace/results && chmod +x /workspace/run_local.sh /workspace/services/agents_api/agent.py /workspace/services/agents_api/server.py

EXPOSE 8000

CMD ["python3", "services/agents_api/server.py", "--host", "0.0.0.0", "--port", "8000"]
