FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends squid ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/runtime-egress/squid.conf /etc/squid/squid.conf
COPY backend/runtime-egress/entrypoint.sh /usr/local/bin/runtime-egress-entrypoint

RUN chown root:proxy /etc/squid/squid.conf \
    && chmod 0640 /etc/squid/squid.conf \
    && chmod 0755 /usr/local/bin/runtime-egress-entrypoint

USER proxy

ENTRYPOINT ["/usr/local/bin/runtime-egress-entrypoint"]
