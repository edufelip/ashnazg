ARG REDROID_BASE=redroid/redroid@sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad
FROM ${REDROID_BASE}

ARG TARGETPLATFORM
ARG SOURCE_SHA
ARG IMAGE_VERSION
ARG AGENT_SHA256
ARG FIXTURE_SHA256

LABEL org.ashnazg.base-image="redroid/redroid@sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad" \
      org.ashnazg.version="${IMAGE_VERSION}" \
      org.ashnazg.source-sha="${SOURCE_SHA}" \
      org.ashnazg.platform="${TARGETPLATFORM}" \
      org.ashnazg.agent-sha256="${AGENT_SHA256}" \
      org.ashnazg.fixture-sha256="${FIXTURE_SHA256}"

COPY --chmod=0644 AshnazgAgent.apk /system/app/AshnazgAgent/AshnazgAgent.apk
COPY --chmod=0644 AshnazgFixture.apk /system/app/AshnazgFixture/AshnazgFixture.apk
COPY --chmod=0644 ashnazg-animation.rc /system/etc/init/ashnazg-animation.rc
COPY --chmod=0644 NOTICES.md /usr/share/doc/ashnazg-redroid/NOTICES.md

# Keep the inherited upstream ENTRYPOINT intact; these args are appended to /init.
CMD ["androidboot.redroid_width=720", "androidboot.redroid_height=1280", "androidboot.redroid_dpi=320", "androidboot.redroid_fps=60", "androidboot.redroid_gpu_mode=guest", "androidboot.use_memfd=1"]
