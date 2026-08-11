FROM caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

LABEL org.opencontainers.image.source="https://github.com/Yaro-lu/camare"

# The listener is unprivileged. Removing the upstream file capability keeps
# execution compatible with cap_drop: ALL and no-new-privileges.
RUN setcap -r /usr/bin/caddy
