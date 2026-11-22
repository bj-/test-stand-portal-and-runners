# Образ исполнителя прогонов: движок и проект монтируются при запуске, в образе только среда.
ARG BASE_REGISTRY=docker.io/library

FROM ${BASE_REGISTRY}/node:24-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-yaml git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g newman@6.2.3 allure@3.20.1 && git config --system --add safe.directory '*'
ENV NODE_PATH=/usr/local/lib/node_modules
