# Pinned by digest; Dependabot proposes new digests. The Python version
# follows the one CI tests on (pyproject.toml), so a version change is a
# reviewed change, never an automatic one.
FROM python:3.14-alpine@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67

# Alpine fixes published after the base image was built, so a scheduled
# rebuild picks them up without a new digest. tunebox uses the standard
# library only, so pip is never run here; removing it keeps its vendored
# packages (and the advisories filed against them) out of the image.
RUN apk upgrade --no-cache \
 && rm -rf /usr/local/lib/python3.*/site-packages/pip \
           /usr/local/lib/python3.*/site-packages/pip-*.dist-info \
           /usr/local/lib/python3.*/ensurepip \
           /usr/local/bin/pip*

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
