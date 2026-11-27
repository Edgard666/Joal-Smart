# Images épinglées par version ET par empreinte : une construction locale donne toujours le
# même Java et le même JOAL. La construction GitHub (.github/workflows/image.yml) remplace ces
# trois valeurs chaque semaine par les dernières versions, puis ne publie qu'après les tests.
ARG JOAL_IMAGE=anthonyraymond/joal:2.1.37@sha256:fc942567c5649b29377e50aa0ca934e4750534d02b4ecf5a0eda3984630f35d7
ARG JDK_IMAGE=eclipse-temurin:21-jdk-noble@sha256:dc333cdac241f073941f043792f2a2f9b790bfe7cd98ef2843c08880986708de
# Ubuntu without Java (v1.33); the Java runtime is reduced by jlink to the modules JOAL uses.
# v1.34: Ubuntu 24.04, i.e. Python 3.12, the version the GitHub build tests with.
ARG BASE_IMAGE=ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b
FROM ${JOAL_IMAGE} AS upstream
FROM ${JDK_IMAGE} AS build
WORKDIR /build
COPY --from=upstream /joal/joal.jar .
COPY telemetry/*.java ./
RUN jar xf joal.jar BOOT-INF \
 && javac --release 17 -proc:none -cp 'BOOT-INF/classes:BOOT-INF/lib/*' -d BOOT-INF/classes UploadTelemetry.java \
 && java --add-exports java.base/jdk.internal.org.objectweb.asm=ALL-UNNAMED RecordTrackerHost.java BOOT-INF/classes/org/araymond/joal/core/ttorrent/client/announcer/tracker/TrackerClientUriProvider.class \
 && java --add-exports java.base/jdk.internal.org.objectweb.asm=ALL-UNNAMED PatchConnectionHandler.java BOOT-INF/classes/org/araymond/joal/core/ttorrent/client/ConnectionHandler.class \
 && jar uf joal.jar BOOT-INF/classes/org/araymond/joal/core/ttorrent/client/announcer/tracker/TrackerClientUriProvider.class BOOT-INF/classes/org/araymond/joal/core/ttorrent/client/ConnectionHandler.class BOOT-INF/classes/org/araymond/joal/telemetry/UploadTelemetry.class
# Java reduced to the modules JOAL (Spring Boot, HTTPS trackers) needs: about 57 MiB instead
# of the full JRE. Same Temurin build and certificates; tested with the real JOAL by ci/smoke_image.py.
RUN jlink --add-modules java.base,java.logging,java.naming,java.management,java.desktop,java.sql,java.xml,java.instrument,jdk.unsupported,jdk.crypto.ec,java.security.jgss,jdk.zipfs,jdk.localedata,java.net.http \
      --include-locales en,fr --strip-debug --no-man-pages --no-header-files --compress=zip-6 --output /build/jre
FROM ${BASE_IMAGE}
ARG JOAL_VERSION=2.1.37
# Empreinte des entrées de la construction (code, JOAL, Java, profils) : la construction
# GitHub ne republie pas une image identique, donc pas de fausse « mise à jour » dans Unraid.
ARG BUILD_INPUTS=local
# Lets update-joal.sh remove this application's old images, and only them.
LABEL app.joal-smart-controller="1" app.joal-smart-controller.joal="${JOAL_VERSION}" \
      app.joal-smart-controller.inputs="${BUILD_INPUTS}"
ENV JAVA_HOME=/opt/java/openjdk PATH=/opt/java/openjdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/jre /opt/java/openjdk
COPY --from=build /build/joal.jar /opt/joal/joal.jar
# Profils .client de JOAL : ajoutés au démarrage dans /data/clients s'ils y manquent (jamais écrasés).
COPY clients/ /opt/joal/clients/
WORKDIR /app
COPY app/ /app/
COPY config.example.json /app/config.example.json
# Journal des versions, pour la fenêtre « Nouveautés » (v1.31).
COPY CHANGELOG.md /app/CHANGELOG.md
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Tolère une extraction Windows (CRLF, bit exécutable perdu).
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod 0755 /usr/local/bin/docker-entrypoint.sh
# Normalize only files shipped in the application image, independently of SMB/ZIP modes.
RUN find /app -type d -exec chmod 0755 {} + && find /app -type f -exec chmod 0644 {} +
USER 99:100
RUN python3 -B -c "import main; from pathlib import Path; Path('static/login.html').read_bytes(); Path('static/index.html').read_bytes(); Path('config.example.json').read_bytes()"
USER root
ENV PYTHONUNBUFFERED=1 PORT=8788 PUID=99 PGID=100 HOME=/tmp JOAL_VERSION=${JOAL_VERSION}
EXPOSE 8788
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/healthz', timeout=4)"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "-u", "/app/main.py"]
