package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"camera-portal/internal/portal"
	webassets "camera-portal/web"
)

func main() {
	healthcheck := flag.String("healthcheck", "", "check an HTTP health endpoint and exit")
	flag.Parse()
	if *healthcheck != "" {
		runHealthcheck(*healthcheck)
		return
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	store, err := portal.OpenStore(env("STATE_PATH", "/data/state.json"), time.Now)
	if err != nil {
		logger.Error("open state", "error", err)
		os.Exit(1)
	}
	cameras := parseCameras(env("CAMERAS", "ptz_view=云台画面,fixed_view=枪机画面"))
	cameraGroups, err := portal.ParseCameraGroups(
		env("CAMERA_GROUPS", "yard=院子:ptz_view|fixed_view;front=门前:|"),
		cameras,
	)
	if err != nil {
		logger.Error("configure camera groups", "error", err)
		os.Exit(1)
	}
	config := portal.Config{
		PublicBaseURL:              os.Getenv("PUBLIC_BASE_URL"),
		LocalBaseURL:               os.Getenv("LOCAL_BASE_URL"),
		LocalAccessKey:             os.Getenv("LOCAL_ACCESS_KEY"),
		AdminOrigin:                os.Getenv("ADMIN_ORIGIN"),
		AdminLogin:                 os.Getenv("ADMIN_TAILSCALE_LOGIN"),
		FrigateURL:                 env("FRIGATE_URL", "http://frigate:5000"),
		Go2RTCURL:                  env("GO2RTC_URL", "http://frigate:1984"),
		RecordingTimezone:          env("RECORDING_TIMEZONE", "Asia/Shanghai"),
		Cameras:                    cameras,
		CameraGroups:               cameraGroups,
		MaxLiveStreams:             envInt("MAX_LIVE_STREAMS", 8),
		MaxLiveStreamsPerDevice:    envInt("MAX_DEVICE_LIVE_STREAMS", 2),
		LiveLease:                  envDuration("LIVE_LEASE", 15*time.Minute),
		MaxReplayRequests:          envInt("MAX_REPLAY_REQUESTS", 16),
		MaxReplayRequestsPerDevice: envInt("MAX_DEVICE_REPLAY_REQUESTS", 4),
	}
	app, err := portal.NewApp(config, store, webassets.FS, logger)
	if err != nil {
		logger.Error("configure portal", "error", err)
		os.Exit(1)
	}

	publicServer := newServer(env("PUBLIC_ADDR", ":8080"), app.PublicHandler())
	adminServer := newServer(env("ADMIN_ADDR", ":8081"), app.AdminHandler())
	errorsChannel := make(chan error, 2)
	go serve(logger, "public", publicServer, errorsChannel)
	go serve(logger, "admin", adminServer, errorsChannel)

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	select {
	case signal := <-signals:
		logger.Info("shutdown requested", "signal", signal.String())
	case err := <-errorsChannel:
		logger.Error("server stopped", "error", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = publicServer.Shutdown(ctx)
	_ = adminServer.Shutdown(ctx)
}

func newServer(address string, handler http.Handler) *http.Server {
	return &http.Server{
		Addr:              address,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       75 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
}

func serve(logger *slog.Logger, name string, server *http.Server, results chan<- error) {
	logger.Info("listener started", "name", name, "address", server.Addr)
	err := server.ListenAndServe()
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		results <- fmt.Errorf("%s listener: %w", name, err)
	}
}

func runHealthcheck(endpoint string) {
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get(endpoint)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		fmt.Fprintln(os.Stderr, response.Status)
		os.Exit(1)
	}
}

func parseCameras(value string) []portal.Camera {
	var cameras []portal.Camera
	for _, item := range strings.Split(value, ",") {
		id, name, ok := strings.Cut(item, "=")
		if !ok {
			continue
		}
		cameras = append(cameras, portal.Camera{
			ID:   strings.TrimSpace(id),
			Name: strings.TrimSpace(name),
		})
	}
	return cameras
}

func env(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value, err := time.ParseDuration(strings.TrimSpace(os.Getenv(name)))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}
