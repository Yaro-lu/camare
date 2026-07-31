ARG CADDY_VERSION=2.11.4

FROM caddy:${CADDY_VERSION}-alpine

# The listener is unprivileged. Removing the upstream file capability keeps
# execution compatible with cap_drop: ALL and no-new-privileges.
RUN setcap -r /usr/bin/caddy
