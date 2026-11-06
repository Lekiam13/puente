FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY puente ./puente
RUN pip install --no-cache-dir --root-user-action=ignore . \
    && useradd --system --uid 10001 puente \
    && mkdir /data && chown puente /data

USER puente
ENV PUENTE_DB=/data/puente.db PORT=8070
EXPOSE 8070
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8070/healthz', timeout=3)"

CMD ["python", "-m", "puente.server"]
