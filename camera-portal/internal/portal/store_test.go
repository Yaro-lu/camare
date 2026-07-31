package portal

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func newTestStore(t *testing.T) (*Store, *time.Time) {
	t.Helper()
	now := time.Date(2026, 7, 29, 8, 0, 0, 0, time.UTC)
	store, err := OpenStore(filepath.Join(t.TempDir(), "state.json"), func() time.Time {
		return now
	})
	if err != nil {
		t.Fatal(err)
	}
	return store, &now
}

func TestPairingIsOneTimeAndStateContainsNoRawCredentials(t *testing.T) {
	store, _ := newTestStore(t)
	pairing, token, err := store.CreatePairing("前台 iPhone", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	device, session, err := store.Redeem(token, DeviceMetadata{
		Platform: "iPhone",
		Browser:  "Safari",
	}, "100.64.0.8")
	if err != nil {
		t.Fatal(err)
	}
	if device.Label != "前台 iPhone" || device.Platform != "iPhone" {
		t.Fatalf("unexpected device: %#v", device)
	}
	if _, _, err := store.Redeem(token, DeviceMetadata{}, "100.64.0.9"); !errors.Is(err, ErrPairingUsed) {
		t.Fatalf("second redeem error = %v, want ErrPairingUsed", err)
	}
	if pairing.TokenHash == token {
		t.Fatal("pairing token was not hashed")
	}
	data, err := os.ReadFile(store.path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), token) || strings.Contains(string(data), session) {
		t.Fatal("raw credential leaked into persisted state")
	}
	if got, ok := store.Authenticate(session, false, ""); !ok || got.ID != device.ID {
		t.Fatal("new session did not authenticate")
	}
}

func TestConcurrentRedeemHasExactlyOneWinner(t *testing.T) {
	store, _ := newTestStore(t)
	_, token, err := store.CreatePairing("", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	const attempts = 12
	var wg sync.WaitGroup
	results := make(chan error, attempts)
	for range attempts {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _, err := store.Redeem(token, DeviceMetadata{Platform: "Android"}, "100.64.0.10")
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	successes := 0
	for err := range results {
		if err == nil {
			successes++
		} else if !errors.Is(err, ErrPairingUsed) {
			t.Fatalf("unexpected error: %v", err)
		}
	}
	if successes != 1 {
		t.Fatalf("successful redemptions = %d, want 1", successes)
	}
}

func TestExpiredPairingAndRevokedSession(t *testing.T) {
	store, now := newTestStore(t)
	_, token, err := store.CreatePairing("", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	*now = now.Add(2 * time.Minute)
	if _, _, err := store.Redeem(token, DeviceMetadata{}, ""); !errors.Is(err, ErrPairingExpired) {
		t.Fatalf("expired redeem error = %v", err)
	}

	_, token, err = store.CreatePairing("", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	device, session, err := store.Redeem(token, DeviceMetadata{}, "")
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RevokeDevice(device.ID); err != nil {
		t.Fatal(err)
	}
	if _, ok := store.Authenticate(session, false, ""); ok {
		t.Fatal("revoked session still authenticated")
	}
}

func TestStoreReloadsDurableState(t *testing.T) {
	store, now := newTestStore(t)
	_, token, err := store.CreatePairing("测试设备", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	device, session, err := store.Redeem(token, DeviceMetadata{Platform: "Windows"}, "100.64.0.20")
	if err != nil {
		t.Fatal(err)
	}
	reloaded, err := OpenStore(store.path, func() time.Time { return *now })
	if err != nil {
		t.Fatal(err)
	}
	got, ok := reloaded.Authenticate(session, false, "")
	if !ok || got.ID != device.ID {
		t.Fatal("session did not survive reload")
	}
}

func TestLocalSessionSharesDeviceButNotCredentialScope(t *testing.T) {
	store, now := newTestStore(t)
	_, token, err := store.CreatePairing("同一设备", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	device, publicSession, err := store.Redeem(token, DeviceMetadata{Platform: "iPhone"}, "203.0.113.8")
	if err != nil {
		t.Fatal(err)
	}
	localSession, err := store.IssueLocalSession(device.ID)
	if err != nil {
		t.Fatal(err)
	}
	if localSession == publicSession {
		t.Fatal("local and public credentials are identical")
	}
	if _, ok := store.Authenticate(localSession, false, ""); ok {
		t.Fatal("local credential authenticated in public scope")
	}
	if _, ok := store.AuthenticateLocal(publicSession, false, ""); ok {
		t.Fatal("public credential authenticated in local scope")
	}
	localDevice, ok := store.AuthenticateLocal(localSession, false, "192.168.1.20")
	if !ok || localDevice.ID != device.ID {
		t.Fatalf("local session resolved to wrong device: %#v", localDevice)
	}
	if devices := store.ListDevices(); len(devices) != 1 {
		t.Fatalf("local session created %d devices, want 1", len(devices))
	}

	reloaded, err := OpenStore(store.path, func() time.Time { return *now })
	if err != nil {
		t.Fatal(err)
	}
	reloadedDevice, ok := reloaded.AuthenticateLocal(localSession, false, "")
	if !ok || reloadedDevice.ID != device.ID {
		t.Fatal("local session did not survive reload")
	}

	rotatedSession, err := reloaded.IssueLocalSession(device.ID)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := reloaded.AuthenticateLocal(localSession, false, ""); ok {
		t.Fatal("rotated local credential remained valid")
	}
	if _, ok := reloaded.AuthenticateLocal(rotatedSession, false, ""); !ok {
		t.Fatal("rotated local credential did not authenticate")
	}

	if err := reloaded.RevokeDevice(device.ID); err != nil {
		t.Fatal(err)
	}
	if _, ok := reloaded.Authenticate(publicSession, false, ""); ok {
		t.Fatal("revoked public session still authenticated")
	}
	if _, ok := reloaded.AuthenticateLocal(rotatedSession, false, ""); ok {
		t.Fatal("revoked local session still authenticated")
	}
	if _, err := reloaded.IssueLocalSession(device.ID); !errors.Is(err, ErrDeviceNotFound) {
		t.Fatalf("revoked device local session error = %v, want ErrDeviceNotFound", err)
	}
}
