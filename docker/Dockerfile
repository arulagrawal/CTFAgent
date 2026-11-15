ARG BASE_IMAGE=ubuntu:24.04
FROM mambaorg/micromamba:2.5.0 AS micromamba
FROM node:24-bookworm-slim AS node
FROM ${BASE_IMAGE} AS tools
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 TERM=xterm
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget git vim nano tree file sed xxd openjdk-17-jdk-headless \
    tcpdump tshark nmap netcat-openbsd telnet dnsutils python3 python3-venv python3-dev \
    nodejs npm openssl hashcat john binutils binwalk foremost steghide unzip p7zip-full \
    jq xmlstarlet gdb llvm hashid util-linux bsdextrautils postgresql-client \
    build-essential cmake pkg-config libffi-dev libssl-dev && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/ctf
ENV PATH="/opt/ctf/bin:${PATH}"
RUN pip install --no-cache-dir requests beautifulsoup4 pycryptodome cryptography pillow \
    z3-solver pwntools scapy paramiko sqlparse lxml numpy scipy flask flask-unsign \
    flask-unsign-wordlist itsdangerous angr capstone websockets websocket-client
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN npm install -g js-beautify eslint @babel/core @babel/cli && npm cache clean --force
RUN update-alternatives --install /usr/bin/objdump objdump /usr/bin/llvm-objdump 100 \
    && useradd -m -s /bin/bash ctfuser && mkdir /workspace && chown ctfuser:ctfuser /workspace
WORKDIR /workspace
FROM tools AS sage
COPY --from=micromamba /bin/micromamba /usr/local/bin/micromamba
RUN micromamba create -y -p /opt/sage -c conda-forge sage=10.10 && micromamba clean --all --yes \
    && printf '#!/bin/sh\nexec /usr/local/bin/micromamba run -p /opt/sage sage "$@"\n' > /usr/local/bin/sage && chmod +x /usr/local/bin/sage
USER ctfuser
CMD ["/bin/bash"]
FROM tools AS standard
USER ctfuser
CMD ["/bin/bash"]
