# python:3.14-alpine, pinned by digest; Dependabot proposes updates.
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TUNEBOX_CONFIG=/data/tunebox.toml

WORKDIR /app
COPY tunebox/ tunebox/
COPY web/ web/
COPY web_admin/ web_admin/
COPY tunebox.example.toml ./

USER 1000:1000
VOLUME /data
EXPOSE 8080

# Checks the process only; an unreachable upstream feed is reported in the body, not here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]

CMD ["python", "-m", "tunebox"]
