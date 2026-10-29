FROM golang:1.27.0-bookworm@sha256:ba5ef6614ca131b80a635fc6a7b715d9ee8a7f333debdbb81afb68259c7d48d4
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake g++ make git pkg-config libssl-dev python3 default-mysql-client \
    ca-certificates libgomp1 procps \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONPATH=/opt/mo-runner GOCACHE=/cache/go-build GOMODCACHE=/cache/go-mod \
    GOTOOLCHAIN=local GOMAXPROCS=4 GOFLAGS=-p=4
COPY mo_runner /opt/mo-runner/mo_runner
WORKDIR /task/source
CMD ["python3", "-m", "mo_runner.runtime"]
