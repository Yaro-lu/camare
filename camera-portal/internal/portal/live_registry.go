package portal

import (
	"context"
	"errors"
	"sync"
	"time"
)

var (
	errLiveDeviceLimit        = errors.New("device live stream limit reached")
	errLiveGlobalLimit        = errors.New("global live stream limit reached")
	errLiveDeviceRevoked      = errors.New("device has been revoked")
	errLiveReplacementTimeout = errors.New("previous live stream did not stop")
	errReplayDeviceLimit      = errors.New("device replay request limit reached")
	errReplayGlobalLimit      = errors.New("global replay request limit reached")
)

const liveReplacementTimeout = 5 * time.Second

type liveKey struct {
	deviceID string
	cameraID string
}

type liveEntry struct {
	key    liveKey
	cancel context.CancelFunc
	done   chan struct{}
	once   sync.Once
}

type liveRegistry struct {
	mu           sync.Mutex
	active       map[liveKey]*liveEntry
	deviceCounts map[string]int
	revoked      map[string]struct{}
	maxGlobal    int
	maxPerDevice int
	lease        time.Duration
}

func newLiveRegistry(maxGlobal, maxPerDevice int, lease time.Duration) *liveRegistry {
	return &liveRegistry{
		active:       make(map[liveKey]*liveEntry),
		deviceCounts: make(map[string]int),
		revoked:      make(map[string]struct{}),
		maxGlobal:    maxGlobal,
		maxPerDevice: maxPerDevice,
		lease:        lease,
	}
}

func (r *liveRegistry) Begin(parent context.Context, deviceID, cameraID string) (context.Context, *liveEntry, error) {
	key := liveKey{deviceID: deviceID, cameraID: cameraID}
	for {
		r.mu.Lock()
		if _, revoked := r.revoked[deviceID]; revoked {
			r.mu.Unlock()
			return nil, nil, errLiveDeviceRevoked
		}
		if previous := r.active[key]; previous != nil {
			previous.cancel()
			done := previous.done
			r.mu.Unlock()

			timer := time.NewTimer(liveReplacementTimeout)
			select {
			case <-done:
				if !timer.Stop() {
					select {
					case <-timer.C:
					default:
					}
				}
				continue
			case <-parent.Done():
				if !timer.Stop() {
					select {
					case <-timer.C:
					default:
					}
				}
				return nil, nil, parent.Err()
			case <-timer.C:
				return nil, nil, errLiveReplacementTimeout
			}
		}
		if r.deviceCounts[deviceID] >= r.maxPerDevice {
			r.mu.Unlock()
			return nil, nil, errLiveDeviceLimit
		}
		if len(r.active) >= r.maxGlobal {
			r.mu.Unlock()
			return nil, nil, errLiveGlobalLimit
		}

		ctx, cancel := context.WithTimeout(parent, r.lease)
		entry := &liveEntry{
			key:    key,
			cancel: cancel,
			done:   make(chan struct{}),
		}
		r.active[key] = entry
		r.deviceCounts[deviceID]++
		r.mu.Unlock()
		return ctx, entry, nil
	}
}

func (r *liveRegistry) End(entry *liveEntry) {
	if entry == nil {
		return
	}
	entry.once.Do(func() {
		entry.cancel()
		r.mu.Lock()
		if current := r.active[entry.key]; current == entry {
			delete(r.active, entry.key)
			count := r.deviceCounts[entry.key.deviceID] - 1
			if count > 0 {
				r.deviceCounts[entry.key.deviceID] = count
			} else {
				delete(r.deviceCounts, entry.key.deviceID)
			}
		}
		close(entry.done)
		r.mu.Unlock()
	})
}

func (r *liveRegistry) Revoke(deviceID string) {
	r.mu.Lock()
	r.revoked[deviceID] = struct{}{}
	var cancels []context.CancelFunc
	for key, entry := range r.active {
		if key.deviceID == deviceID {
			cancels = append(cancels, entry.cancel)
		}
	}
	r.mu.Unlock()
	for _, cancel := range cancels {
		cancel()
	}
}

func (r *liveRegistry) activeCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.active)
}

type concurrencyLimiter struct {
	mu           sync.Mutex
	total        int
	byDevice     map[string]int
	maxGlobal    int
	maxPerDevice int
}

func newConcurrencyLimiter(maxGlobal, maxPerDevice int) *concurrencyLimiter {
	return &concurrencyLimiter{
		byDevice:     make(map[string]int),
		maxGlobal:    maxGlobal,
		maxPerDevice: maxPerDevice,
	}
}

func (l *concurrencyLimiter) Acquire(deviceID string) (func(), error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.byDevice[deviceID] >= l.maxPerDevice {
		return nil, errReplayDeviceLimit
	}
	if l.total >= l.maxGlobal {
		return nil, errReplayGlobalLimit
	}
	l.total++
	l.byDevice[deviceID]++

	var once sync.Once
	return func() {
		once.Do(func() {
			l.mu.Lock()
			l.total--
			count := l.byDevice[deviceID] - 1
			if count > 0 {
				l.byDevice[deviceID] = count
			} else {
				delete(l.byDevice, deviceID)
			}
			l.mu.Unlock()
		})
	}, nil
}
