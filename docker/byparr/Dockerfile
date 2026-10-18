# Code-only overlay for the installed Byparr 3.0.4 runtime.
FROM ghcr.io/thephaseless/byparr:3.0.4@sha256:874f719518f617d03a60e03411fc5d090647e1a877041e81f8dc965927c7deb6
USER root
WORKDIR /app
COPY docker/byparr/wait-for-results.patch /tmp/wait-for-results.patch
RUN git apply --check /tmp/wait-for-results.patch \
    && git apply /tmp/wait-for-results.patch \
    && rm /tmp/wait-for-results.patch
COPY docker/byparr/test_wait.py /tmp/test_wait.py
USER 1000
RUN PYTHONPATH=/app /app/.venv/bin/python /tmp/test_wait.py
ARG VCS_REF
LABEL org.opencontainers.image.revision=$VCS_REF \
    org.opencontainers.image.source="https://github.com/pullboxapp/pullbox-direct-providers" \
    org.opencontainers.image.base.name="ghcr.io/thephaseless/byparr:3.0.4" \
    org.opencontainers.image.base.digest="sha256:874f719518f617d03a60e03411fc5d090647e1a877041e81f8dc965927c7deb6"
