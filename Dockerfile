FROM python:3.13-slim

ARG APP_UID=1001

# cairo draws SVG reference images (CairoSVG); tini is the init process (see ENTRYPOINT).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcairo2 tini \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --uid "$APP_UID" --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir /data && chown app:app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % (os.environ.get('PORT', '').strip() or 8000), timeout=4)"]
# An init process, so that no docker run option is needed for one: it passes docker stop's
# signal on to the server, which then marks the generations it interrupts as failed.
# -s keeps it working when Docker's own init runs first (--init).
ENTRYPOINT ["tini", "-s", "--"]
CMD ["python", "-m", "pixdrop"]
