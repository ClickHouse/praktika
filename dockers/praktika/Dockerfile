# docker build -t clickhouse/praktika .
FROM ubuntu:22.04

RUN apt-get update && env DEBIAN_FRONTEND=noninteractive apt-get install --yes \
        python3-pip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/debconf /tmp/*
