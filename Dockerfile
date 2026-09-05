# freellmpool — OpenAI-compatible gateway over free LLM tiers.
#
#   docker build -t freellmpool:local .
#   docker run -p 127.0.0.1:8080:8080 freellmpool:local
#
# A fresh container serves liveness with no eligible inference routes. Mount
# the managed configuration/state directory after setup and discovery; inspect
# authenticated /readyz for capacity. When exposing the proxy beyond localhost,
# set FREELLMPOOL_PROXY_KEY to require a Bearer token.
# For a different published port, run proxy with --allowed-authority HOST:PORT
# matching the client URL, as well as --host 0.0.0.0 --port 8080 --allow-lan.
FROM python:3.14-alpine@sha256:c6ead215bfd31f1e433d968853b7a769989117115b728874824e6c0a27cb96fc

WORKDIR /app
RUN apk upgrade --no-cache
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir \
    "anyio==4.14.2" \
    "certifi==2026.7.22" \
    "h11==0.16.0" \
    "httpcore==1.0.9" \
    "httpx==0.28.1" \
    "idna==3.19" \
    . \
    && python -m pip uninstall --yes pip setuptools wheel

RUN adduser -D -u 10001 freellmpool
USER freellmpool

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/livez', timeout=2)"
ENTRYPOINT ["freellmpool"]
CMD ["proxy", "--host", "0.0.0.0", "--port", "8080", "--allow-lan", "--allow-no-auth", "--allowed-authority", "127.0.0.1:8080", "--allowed-authority", "localhost:8080"]
