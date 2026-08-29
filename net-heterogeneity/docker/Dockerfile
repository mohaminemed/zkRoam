FROM hyperledger/besu:24.7.0

USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends iproute2 && \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
