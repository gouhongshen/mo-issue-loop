FROM golang:1.27.1-bookworm@sha256:8d48e12ec56735e9358640898b9d9b9fcca110612ed8a5567438c0a1baa24e66
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake g++ make git pkg-config libssl-dev python3 default-mysql-client \
    ca-certificates libgomp1 procps bzip2 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONPATH=/opt/mo-runner GOCACHE=/cache/go-build GOMODCACHE=/cache/go-mod \
    GOTOOLCHAIN=local GOMAXPROCS=4 GOFLAGS=-p=4
COPY mo_runner /opt/mo-runner/mo_runner
WORKDIR /task/source
CMD ["python3", "-m", "mo_runner.runtime"]
