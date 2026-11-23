# syntax=docker/dockerfile:1
# Sample multi-stage Dockerfile exercising the Dockerfile parser. Names are synthetic.
ARG BASE_IMAGE=registry.example.com/base/python:3.12
ARG RUNTIME_TAG=1.0

FROM --platform=linux/amd64 registry.example.com/base/python:3.12 AS build
ENV API_KEY=not-a-real-value
WORKDIR /src
# Install the dependencies in one layer.
RUN pip install \
    --no-cache-dir \
    wheel
COPY . /src

FROM build AS test
RUN pytest

FROM ${BASE_IMAGE} AS configurable
COPY --from=build /src/dist /app

FROM registry.example.com:5000/base/runtime:1.0@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
COPY --from=build /src/dist /opt/app
COPY --from=0 /src/config /opt/config
COPY --from=acme/tool:1.2 /usr/bin/tool /usr/bin/tool
LABEL owner="not-a-real-value"
