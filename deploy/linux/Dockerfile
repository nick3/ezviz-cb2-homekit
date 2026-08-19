# syntax=docker/dockerfile:1

ARG GO_VERSION=1.25-bookworm
ARG PYTHON_VERSION=3.12-slim-bookworm

FROM --platform=$BUILDPLATFORM golang:${GO_VERSION} AS go2rtc-build
ARG TARGETOS
ARG TARGETARCH

ENV GOOS=${TARGETOS} \
    GOARCH=${TARGETARCH} \
    CGO_ENABLED=0

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -trimpath -ldflags="-s -w" -o /out/go2rtc .


FROM python:${PYTHON_VERSION} AS pyezviz-build
ARG PYEZVIZ_REPOSITORY=https://github.com/liuzexier/pyEzvizApiCN.git
ARG PYEZVIZ_COMMIT=717a768185ccbb92f09eacf3f9273352696d8a91

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --filter=blob:none "${PYEZVIZ_REPOSITORY}" pyEzvizApiCN \
    && git -C pyEzvizApiCN checkout --quiet --detach "${PYEZVIZ_COMMIT}"
COPY scripts/patches/ /patches/
COPY deploy/linux/requirements.lock /requirements.lock
RUN for patch_file in /patches/*.patch; do \
        git -C pyEzvizApiCN apply "${patch_file}"; \
    done \
    && python -m pip wheel --disable-pip-version-check --wheel-dir /wheels \
         -r /requirements.lock \
    && python -m pip wheel --disable-pip-version-check --no-deps \
         --wheel-dir /wheels ./pyEzvizApiCN


FROM python:${PYTHON_VERSION}

RUN apt-get update \
    && packages="ca-certificates ffmpeg libseccomp2 mesa-va-drivers tini tzdata" \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
         packages="${packages} intel-media-va-driver"; \
       fi \
    && apt-get install -y --no-install-recommends ${packages} \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pyezviz-build /wheels/ /wheels/
RUN python -m pip install --disable-pip-version-check --no-cache-dir \
      --no-index --find-links=/wheels pyezvizapi \
    && rm -rf /wheels

COPY --from=go2rtc-build /out/go2rtc /usr/local/bin/go2rtc
WORKDIR /app
COPY scripts/probe-ezviz-direct-reverse.py /app/scripts/
COPY scripts/ezviz_warm_controller.py /app/scripts/
COPY scripts/ezviz_direct_media.py /app/scripts/
COPY scripts/ezviz_network_lock.py /app/scripts/
COPY scripts/login-ezviz-cloud.py /app/scripts/
COPY deploy/linux/ /app/deploy/linux/

RUN chmod 0755 \
      /usr/local/bin/go2rtc \
      /app/scripts/probe-ezviz-direct-reverse.py \
      /app/scripts/ezviz_warm_controller.py \
      /app/scripts/login-ezviz-cloud.py \
      /app/deploy/linux/entrypoint.sh \
      /app/deploy/linux/manage.sh \
      /app/deploy/linux/make-bundle.sh

ARG TARGETARCH
ARG BUILDARCH
RUN if [ -z "${TARGETARCH}" ] || [ "${TARGETARCH}" = "${BUILDARCH}" ]; then \
      PYTHONPATH=/app/scripts python3 /app/deploy/linux/seccomp_selftest.py; \
    else \
      echo "Skipping seccomp execution test during cross-architecture build"; \
    fi

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EZVIZ_DATA_DIR=/data \
    FFMPEG_BIN=/usr/bin/ffmpeg

EXPOSE 1984/tcp 8443/udp 39000/tcp
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/tini", "--", "/app/deploy/linux/entrypoint.sh"]
CMD ["start"]
