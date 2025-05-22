FROM python:3.11-slim

WORKDIR /app
COPY . .

# Installa dipendenze di sistema necessarie per il monitoraggio di rete, log, Docker e temperatura
RUN apt-get update && apt-get install -y \
    sudo \
    rsyslog \
    procps \
    iproute2 \
    net-tools \
    lsof \
    iputils-ping \
    hostname \
    curl \
    ca-certificates \
    gnupg \
    lm-sensors \
    && rm -rf /var/lib/apt/lists/*

# Installa Docker CLI per poter comunicare con il socket Docker dell'host
RUN install -m 0755 -d /etc/apt/keyrings
RUN curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
RUN chmod a+r /etc/apt/keyrings/docker.gpg
RUN echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null
RUN apt-get update && apt-get install -y docker-ce-cli && rm -rf /var/lib/apt/lists/*

RUN pip install -r requirements.txt

# Crea le directory necessarie per i file di stato
RUN mkdir -p /tmp

EXPOSE 5000
CMD ["python", "server_monitor.py"]