FROM python:3.12-slim-bookworm

RUN useradd -m -u 1000 nodeagent
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY node_agent/ ./node_agent/

RUN mkdir -p /app/var && chown nodeagent:nodeagent /app/var

USER nodeagent
CMD ["python", "-m", "node_agent"]
