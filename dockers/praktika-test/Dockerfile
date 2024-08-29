# docker build -t clickhouse/praktika-test .
ARG FROM_TAG=latest
FROM clickhouse/praktika:$FROM_TAG

COPY requirements.txt /
RUN pip3 install --no-cache-dir -r requirements.txt
