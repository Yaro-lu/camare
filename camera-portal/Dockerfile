FROM node:22.19.0-alpine3.22 AS webdeps

RUN npm install --prefix /deps --ignore-scripts --no-audit --no-fund \
    hls.js@1.6.16 \
    @cycjimmy/jsmpeg-player@6.1.2

FROM golang:1.26.5-alpine3.24 AS build

WORKDIR /src
RUN apk add --no-cache build-base
COPY go.mod go.sum ./
RUN go mod download

COPY . .
COPY --from=webdeps /deps/node_modules/hls.js/dist/hls.min.js /src/web/assets/hls.min.js
COPY --from=webdeps /deps/node_modules/@cycjimmy/jsmpeg-player/dist/jsmpeg-player.umd.min.js /src/web/assets/jsmpeg-player.min.js

RUN GOMAXPROCS=1 go test -p=1 ./... \
    && GOMAXPROCS=1 go test -p=1 -race ./internal/portal \
    && CGO_ENABLED=0 GOMAXPROCS=1 go build -p=1 -trimpath -ldflags="-s -w" -o /out/camera-portal ./cmd/camera-portal

FROM scratch

COPY --from=build /out/camera-portal /camera-portal

USER 65532:65532
EXPOSE 8080 8081
ENTRYPOINT ["/camera-portal"]
