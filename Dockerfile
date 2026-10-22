# The control plane: the web app, the agent API and MCP. It holds the model key and the
# owner's signing key, and starts one sandbox per run through the host's Docker engine.
# It never runs a browser itself.
FROM python:3.13-slim
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
RUN pip install --no-cache-dir uv==0.9.*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY countersign ./countersign
COPY sandbox/seccomp_chromium.json ./sandbox/seccomp_chromium.json
ENV PATH=/app/.venv/bin:$PATH CS_DATA=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["python", "-m", "countersign.web.app"]
