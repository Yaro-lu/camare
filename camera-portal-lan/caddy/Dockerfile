FROM golang:1.26.5-alpine3.24@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 AS build

ENV GOTOOLCHAIN=local

WORKDIR /src

# Build Caddy as a dependency so its version metadata remains available. The
# two explicit upgrades are the first patched releases identified by Trivy.
RUN set -eux; \
	go mod init camare.local/caddy-build; \
	go get github.com/caddyserver/caddy/v2@v2.11.4; \
	cp "$(go env GOMODCACHE)/github.com/caddyserver/caddy/v2@v2.11.4/cmd/caddy/main.go" ./main.go; \
	go get golang.org/x/text@v0.39.0 google.golang.org/grpc@v1.82.1; \
	go mod tidy; \
	test "$(go list -m -f '{{.Version}}' golang.org/x/text)" = "v0.39.0"; \
	test "$(go list -m -f '{{.Version}}' google.golang.org/grpc)" = "v1.82.1"; \
	go mod verify; \
	CGO_ENABLED=0 go build -mod=readonly -trimpath -buildvcs=false -ldflags="-s -w" -o /out/caddy .

FROM caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

LABEL org.opencontainers.image.source="https://github.com/Yaro-lu/camare"

# curl is only present for convenience in the upstream runtime image. BusyBox
# wget provides this stack's healthcheck, so remove curl and its orphaned
# libcurl/c-ares dependencies before installing the rebuilt static binary.
RUN set -eux; \
	apk del --no-network curl; \
	for package in curl libcurl c-ares; do \
		if apk info --exists "$package"; then \
			echo "unexpected runtime package: $package" >&2; \
			exit 1; \
		fi; \
	done; \
	rm -f /usr/bin/caddy

COPY --from=build /out/caddy /usr/bin/caddy

# The listener is unprivileged. Replacing the upstream executable drops its
# file capability and keeps cap_drop: ALL / no-new-privileges effective.
RUN set -eux; \
	test -z "$(getcap /usr/bin/caddy)"; \
	caddy version
