package portal

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestLiveRegistryReplacesSameDeviceCameraWithoutUsingExtraSlot(t *testing.T) {
	registry := newLiveRegistry(2, 2, time.Minute)
	firstContext, first, err := registry.Begin(context.Background(), "device-1", "camera-1")
	if err != nil {
		t.Fatal(err)
	}
	firstEnded := make(chan struct{})
	go func() {
		<-firstContext.Done()
		registry.End(first)
		close(firstEnded)
	}()

	_, second, err := registry.Begin(context.Background(), "device-1", "camera-1")
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-firstEnded:
	case <-time.After(time.Second):
		t.Fatal("replaced stream was not cancelled")
	}
	if got := registry.activeCount(); got != 1 {
		t.Fatalf("active streams = %d, want 1", got)
	}
	registry.End(second)
}

func TestLiveRegistryEnforcesPerDeviceAndGlobalLimits(t *testing.T) {
	registry := newLiveRegistry(3, 2, time.Minute)
	_, first, err := registry.Begin(context.Background(), "device-1", "camera-1")
	if err != nil {
		t.Fatal(err)
	}
	defer registry.End(first)
	_, second, err := registry.Begin(context.Background(), "device-1", "camera-2")
	if err != nil {
		t.Fatal(err)
	}
	defer registry.End(second)
	if _, _, err := registry.Begin(context.Background(), "device-1", "camera-3"); !errors.Is(err, errLiveDeviceLimit) {
		t.Fatalf("third device stream error = %v, want per-device limit", err)
	}

	_, third, err := registry.Begin(context.Background(), "device-2", "camera-1")
	if err != nil {
		t.Fatal(err)
	}
	defer registry.End(third)
	if _, _, err := registry.Begin(context.Background(), "device-3", "camera-1"); !errors.Is(err, errLiveGlobalLimit) {
		t.Fatalf("fourth global stream error = %v, want global limit", err)
	}
}

func TestLiveRegistryLeaseAndRevocationCancelConnections(t *testing.T) {
	registry := newLiveRegistry(2, 2, 25*time.Millisecond)
	leaseContext, entry, err := registry.Begin(context.Background(), "device-1", "camera-1")
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-leaseContext.Done():
	case <-time.After(time.Second):
		t.Fatal("live lease did not expire")
	}
	registry.End(entry)

	activeContext, active, err := registry.Begin(context.Background(), "device-2", "camera-1")
	if err != nil {
		t.Fatal(err)
	}
	registry.Revoke("device-2")
	select {
	case <-activeContext.Done():
	case <-time.After(time.Second):
		t.Fatal("revocation did not cancel active stream")
	}
	if _, _, err := registry.Begin(context.Background(), "device-2", "camera-2"); !errors.Is(err, errLiveDeviceRevoked) {
		t.Fatalf("revoked device begin error = %v", err)
	}
	registry.End(active)
}

func TestReplayLimiterBoundsEachDeviceAndGlobalConcurrency(t *testing.T) {
	limiter := newConcurrencyLimiter(3, 2)
	releaseOne, err := limiter.Acquire("device-1")
	if err != nil {
		t.Fatal(err)
	}
	releaseTwo, err := limiter.Acquire("device-1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := limiter.Acquire("device-1"); !errors.Is(err, errReplayDeviceLimit) {
		t.Fatalf("third device replay request error = %v", err)
	}
	releaseThree, err := limiter.Acquire("device-2")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := limiter.Acquire("device-3"); !errors.Is(err, errReplayGlobalLimit) {
		t.Fatalf("fourth global replay request error = %v", err)
	}

	releaseOne()
	releaseOne()
	if release, err := limiter.Acquire("device-1"); err != nil {
		t.Fatalf("released replay slot was not reusable: %v", err)
	} else {
		release()
	}
	releaseTwo()
	releaseThree()
}

func TestReplayLimiterBoundsConcurrentRequestsWithinOneSession(t *testing.T) {
	limiter := newConcurrencyLimiter(2, 1)
	releases := make([]func(), 0, maxConcurrentReplaySessionSubrequests)
	for i := 0; i < maxConcurrentReplaySessionSubrequests; i++ {
		release, err := limiter.AcquireSession("device-1", "camera-1/100/200")
		if err != nil {
			t.Fatalf("session subrequest %d error = %v", i+1, err)
		}
		releases = append(releases, release)
	}
	if _, err := limiter.AcquireSession("device-1", "camera-1/100/200"); !errors.Is(err, errReplaySessionLimit) {
		t.Fatalf("excess session subrequest error = %v", err)
	}

	releases[0]()
	releases[0]()
	replacement, err := limiter.AcquireSession("device-1", "camera-1/100/200")
	if err != nil {
		t.Fatalf("released session subrequest was not reusable: %v", err)
	}
	replacement()
	for _, release := range releases[1:] {
		release()
	}
}
