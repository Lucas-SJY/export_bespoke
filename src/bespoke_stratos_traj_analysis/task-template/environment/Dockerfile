FROM ubuntu:24.04

# tests/test.sh validates the span intervals in Python, so the image needs a
# real interpreter. NOT python3-minimal: that package ships without the
# standard library, so `import json` fails and every trial scores 0 on a
# verifier crash rather than on its own output.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /output /logs

COPY trajectory.json /trajectory.json

WORKDIR /workspace
