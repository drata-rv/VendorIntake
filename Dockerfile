FROM python:3.13.7-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.13.7-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
RUN groupadd -g 10001 bridge \
 && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin bridge \
 && mkdir /data && chown 10001:10001 /data
COPY --from=build /opt/venv /opt/venv
WORKDIR /srv
COPY app ./app
COPY migrations ./migrations
COPY docker-entrypoint.sh ./
USER 10001:10001
VOLUME /data
EXPOSE 8000
ENTRYPOINT ["/srv/docker-entrypoint.sh"]
