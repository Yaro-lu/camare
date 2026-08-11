FROM node:26.3.0-alpine3.22@sha256:c7932b9e5e337b0e733d6e16abc1b0e104759e8b05e59ed56586cce967d26dfe AS webdeps

WORKDIR /deps
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

FROM golang:1.26.5-alpine3.24@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 AS build

WORKDIR /src
RUN apk add --no-cache build-base
COPY go.mod go.sum ./
RUN go mod download && go mod verify

COPY . .
COPY --from=webdeps /deps/node_modules/hls.js/dist/hls.min.js /src/web/assets/hls.min.js
COPY --from=webdeps /deps/node_modules/@cycjimmy/jsmpeg-player/dist/jsmpeg-player.umd.min.js /src/web/assets/jsmpeg-player.min.js

RUN GOMAXPROCS=1 go test -p=1 ./... \
    && GOMAXPROCS=1 go test -p=1 -race ./internal/portal \
    && CGO_ENABLED=0 GOMAXPROCS=1 go build -p=1 -trimpath -ldflags="-s -w" -o /out/camera-portal ./cmd/camera-portal

FROM scratch

LABEL org.opencontainers.image.source="https://github.com/Yaro-lu/camare"

COPY --from=build /out/camera-portal /camera-portal

USER 65532:65532
EXPOSE 8080 8081
ENTRYPOINT ["/camera-portal"]
