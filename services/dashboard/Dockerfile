FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace

COPY services/dashboard /workspace/services/dashboard
COPY core/trace/ecr_trace.py /workspace/core/trace/ecr_trace.py
COPY core/trace/maestro_compiler.py /workspace/core/trace/maestro_compiler.py
COPY core/code_intel /workspace/core/code_intel
COPY services/agents_api/schemas.py /workspace/services/agents_api/schemas.py
COPY config/personas.json /workspace/config/personas.json
COPY briefs /workspace/briefs

RUN mkdir -p /workspace/results

EXPOSE 8765

CMD ["python3", "services/dashboard/server.py", "results", "--host", "0.0.0.0", "--port", "8765", "--no-browser", "--allow-empty"]
