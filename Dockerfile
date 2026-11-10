FROM python:3.13-slim

# cairo draws SVG reference images (CairoSVG); tini is the init process (see ENTRYPOINT).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcairo2 tini \
 && rm -rf /var/lib/apt/lists/*

# The server runs unprivileged as UID/GID 61001: above the range Linux gives to real accounts
# (up to 60000), so it matches no login of the host, such as uid 1000, which often has
# passwordless sudo. A new named volume takes the ownership of /data when it is first
# mounted; a host directory must be given to 61001 (docs/DEPLOYMENT.md).
RUN groupadd --system --gid 61001 app \
 && useradd --system --uid 61001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir /data && chown app:app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# Numeric, so that platforms such as Kubernetes can check that it is not root.
USER 61001:61001
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % (os.environ.get('PORT', '').strip() or 8000), timeout=4)"]
# An init process, so that no docker run option is needed for one: it passes docker stop's
# signal on to the server, which then marks the generations it interrupts as failed, and
# reaps processes orphaned in the container, which would stay as zombies under the server.
# -s keeps it reaping when Docker's own init runs first (--init).
ENTRYPOINT ["tini", "-s", "--"]
CMD ["python", "-m", "pixdrop"]
