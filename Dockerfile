FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY jev_mail ./jev_mail
RUN pip install . \
    && useradd --uid 1000 --create-home jev \
    && mkdir /data \
    && chown jev /data

USER 1000
VOLUME /data

ENTRYPOINT ["jev-mail", "--dir", "/data"]
CMD ["watch"]
