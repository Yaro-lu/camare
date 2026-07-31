package portal

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

var (
	ErrPairingInvalid = errors.New("pairing code is invalid")
	ErrPairingExpired = errors.New("pairing code has expired")
	ErrPairingUsed    = errors.New("pairing code has already been used")
	ErrDeviceNotFound = errors.New("device not found")
)

type DeviceMetadata struct {
	Platform   string `json:"platform"`
	OS         string `json:"os"`
	Browser    string `json:"browser"`
	Model      string `json:"model"`
	FormFactor string `json:"form_factor"`
	Screen     string `json:"screen"`
	Timezone   string `json:"timezone"`
	Language   string `json:"language"`
	UserAgent  string `json:"user_agent"`
}

type Device struct {
	ID         string     `json:"id"`
	Label      string     `json:"label"`
	Platform   string     `json:"platform"`
	OS         string     `json:"os"`
	Browser    string     `json:"browser"`
	Model      string     `json:"model"`
	FormFactor string     `json:"form_factor"`
	Screen     string     `json:"screen"`
	Timezone   string     `json:"timezone"`
	Language   string     `json:"language"`
	UserAgent  string     `json:"user_agent,omitempty"`
	FirstSeen  time.Time  `json:"first_seen"`
	LastSeen   time.Time  `json:"last_seen"`
	LastIP     string     `json:"last_ip"`
	RevokedAt  *time.Time `json:"revoked_at,omitempty"`
}

type Pairing struct {
	ID         string     `json:"id"`
	TokenHash  string     `json:"token_hash"`
	Label      string     `json:"label"`
	CreatedAt  time.Time  `json:"created_at"`
	ExpiresAt  time.Time  `json:"expires_at"`
	RedeemedAt *time.Time `json:"redeemed_at,omitempty"`
	DeviceID   string     `json:"device_id,omitempty"`
}

type persistedState struct {
	Version       int                `json:"version"`
	Pairings      map[string]Pairing `json:"pairings"`
	Devices       map[string]Device  `json:"devices"`
	Sessions      map[string]string  `json:"sessions"`
	LocalSessions map[string]string  `json:"local_sessions,omitempty"`
}

type Store struct {
	mu    sync.RWMutex
	path  string
	now   func() time.Time
	state persistedState
}

func OpenStore(path string, now func() time.Time) (*Store, error) {
	if now == nil {
		now = time.Now
	}
	s := &Store{
		path: path,
		now:  now,
		state: persistedState{
			Version:       1,
			Pairings:      map[string]Pairing{},
			Devices:       map[string]Device{},
			Sessions:      map[string]string{},
			LocalSessions: map[string]string{},
		},
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read state: %w", err)
	}
	if err := json.Unmarshal(data, &s.state); err != nil {
		return nil, fmt.Errorf("decode state: %w", err)
	}
	if s.state.Version != 1 {
		return nil, fmt.Errorf("unsupported state version %d", s.state.Version)
	}
	if s.state.Pairings == nil {
		s.state.Pairings = map[string]Pairing{}
	}
	if s.state.Devices == nil {
		s.state.Devices = map[string]Device{}
	}
	if s.state.Sessions == nil {
		s.state.Sessions = map[string]string{}
	}
	if s.state.LocalSessions == nil {
		s.state.LocalSessions = map[string]string{}
	}
	return s, nil
}

func (s *Store) CreatePairing(label string, ttl time.Duration) (Pairing, string, error) {
	if ttl <= 0 || ttl > 7*24*time.Hour {
		ttl = 24 * time.Hour
	}
	raw, err := randomCredential(32)
	if err != nil {
		return Pairing{}, "", err
	}
	id, err := randomCredential(12)
	if err != nil {
		return Pairing{}, "", err
	}
	now := s.now().UTC()
	pairing := Pairing{
		ID:        id,
		TokenHash: hashCredential(raw),
		Label:     cleanText(label, 80),
		CreatedAt: now,
		ExpiresAt: now.Add(ttl),
	}
	err = s.update(func(next *persistedState) error {
		next.Pairings[pairing.TokenHash] = pairing
		return nil
	})
	return pairing, raw, err
}

func (s *Store) PairingStatus(raw string) (Pairing, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	pairing, ok := s.state.Pairings[hashCredential(raw)]
	if !ok {
		return Pairing{}, ErrPairingInvalid
	}
	if pairing.RedeemedAt != nil {
		return pairing, ErrPairingUsed
	}
	if !s.now().UTC().Before(pairing.ExpiresAt) {
		return pairing, ErrPairingExpired
	}
	return pairing, nil
}

func (s *Store) Redeem(raw string, metadata DeviceMetadata, ip string) (Device, string, error) {
	session, err := randomCredential(32)
	if err != nil {
		return Device{}, "", err
	}
	deviceID, err := randomCredential(12)
	if err != nil {
		return Device{}, "", err
	}
	var device Device
	err = s.update(func(next *persistedState) error {
		key := hashCredential(raw)
		pairing, ok := next.Pairings[key]
		if !ok {
			return ErrPairingInvalid
		}
		now := s.now().UTC()
		if pairing.RedeemedAt != nil {
			return ErrPairingUsed
		}
		if !now.Before(pairing.ExpiresAt) {
			return ErrPairingExpired
		}
		device = Device{
			ID:         deviceID,
			Label:      firstNonEmpty(cleanText(pairing.Label, 80), defaultDeviceLabel(metadata)),
			Platform:   cleanText(metadata.Platform, 40),
			OS:         cleanText(metadata.OS, 80),
			Browser:    cleanText(metadata.Browser, 80),
			Model:      cleanText(metadata.Model, 120),
			FormFactor: cleanText(metadata.FormFactor, 30),
			Screen:     cleanText(metadata.Screen, 40),
			Timezone:   cleanText(metadata.Timezone, 80),
			Language:   cleanText(metadata.Language, 30),
			UserAgent:  cleanText(metadata.UserAgent, 500),
			FirstSeen:  now,
			LastSeen:   now,
			LastIP:     cleanText(ip, 80),
		}
		pairing.RedeemedAt = &now
		pairing.DeviceID = device.ID
		next.Pairings[key] = pairing
		next.Devices[device.ID] = device
		next.Sessions[hashCredential(session)] = device.ID
		return nil
	})
	if err != nil {
		return Device{}, "", err
	}
	return device, session, nil
}

func (s *Store) Authenticate(session string, touch bool, ip string) (Device, bool) {
	return s.authenticate(session, touch, ip, false)
}

func (s *Store) AuthenticateLocal(session string, touch bool, ip string) (Device, bool) {
	return s.authenticate(session, touch, ip, true)
}

func (s *Store) authenticate(session string, touch bool, ip string, local bool) (Device, bool) {
	if session == "" {
		return Device{}, false
	}
	key := hashCredential(session)
	s.mu.RLock()
	sessions := s.state.Sessions
	if local {
		sessions = s.state.LocalSessions
	}
	deviceID, ok := sessions[key]
	device, found := s.state.Devices[deviceID]
	s.mu.RUnlock()
	if !ok || !found || device.RevokedAt != nil {
		return Device{}, false
	}
	if touch && s.now().UTC().Sub(device.LastSeen) >= 10*time.Minute {
		_ = s.update(func(next *persistedState) error {
			current, exists := next.Devices[deviceID]
			if !exists || current.RevokedAt != nil {
				return nil
			}
			current.LastSeen = s.now().UTC()
			current.LastIP = cleanText(ip, 80)
			next.Devices[deviceID] = current
			device = current
			return nil
		})
	}
	return device, true
}

func (s *Store) IssueLocalSession(deviceID string) (string, error) {
	session, err := randomCredential(32)
	if err != nil {
		return "", err
	}
	err = s.update(func(next *persistedState) error {
		device, ok := next.Devices[deviceID]
		if !ok || device.RevokedAt != nil {
			return ErrDeviceNotFound
		}
		if next.LocalSessions == nil {
			next.LocalSessions = map[string]string{}
		}
		for key, currentDeviceID := range next.LocalSessions {
			if currentDeviceID == deviceID {
				delete(next.LocalSessions, key)
			}
		}
		next.LocalSessions[hashCredential(session)] = deviceID
		return nil
	})
	if err != nil {
		return "", err
	}
	return session, nil
}

func (s *Store) ListDevices() []Device {
	s.mu.RLock()
	defer s.mu.RUnlock()
	items := make([]Device, 0, len(s.state.Devices))
	for _, device := range s.state.Devices {
		device.UserAgent = ""
		items = append(items, device)
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].FirstSeen.After(items[j].FirstSeen)
	})
	return items
}

func (s *Store) ListPairings() []Pairing {
	s.mu.RLock()
	defer s.mu.RUnlock()
	items := make([]Pairing, 0, len(s.state.Pairings))
	for _, pairing := range s.state.Pairings {
		pairing.TokenHash = ""
		items = append(items, pairing)
	}
	sort.Slice(items, func(i, j int) bool {
		return items[i].CreatedAt.After(items[j].CreatedAt)
	})
	return items
}

func (s *Store) RenameDevice(id, label string) (Device, error) {
	var result Device
	err := s.update(func(next *persistedState) error {
		device, ok := next.Devices[id]
		if !ok {
			return ErrDeviceNotFound
		}
		device.Label = cleanText(label, 80)
		if device.Label == "" {
			return errors.New("device label is required")
		}
		next.Devices[id] = device
		result = device
		return nil
	})
	return result, err
}

func (s *Store) RevokeDevice(id string) error {
	return s.update(func(next *persistedState) error {
		device, ok := next.Devices[id]
		if !ok {
			return ErrDeviceNotFound
		}
		if device.RevokedAt == nil {
			now := s.now().UTC()
			device.RevokedAt = &now
			next.Devices[id] = device
		}
		for key, deviceID := range next.Sessions {
			if deviceID == id {
				delete(next.Sessions, key)
			}
		}
		for key, deviceID := range next.LocalSessions {
			if deviceID == id {
				delete(next.LocalSessions, key)
			}
		}
		return nil
	})
}

func (s *Store) update(change func(*persistedState) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	next, err := cloneState(s.state)
	if err != nil {
		return err
	}
	if err := change(&next); err != nil {
		return err
	}
	if err := s.save(next); err != nil {
		return err
	}
	s.state = next
	return nil
}

func (s *Store) save(state persistedState) error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return fmt.Errorf("create state directory: %w", err)
	}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("encode state: %w", err)
	}
	temp, err := os.CreateTemp(filepath.Dir(s.path), ".state-*.tmp")
	if err != nil {
		return fmt.Errorf("create temporary state: %w", err)
	}
	tempName := temp.Name()
	defer os.Remove(tempName)
	if err := temp.Chmod(0o600); err != nil {
		temp.Close()
		return err
	}
	if _, err := temp.Write(data); err != nil {
		temp.Close()
		return fmt.Errorf("write state: %w", err)
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return fmt.Errorf("sync state: %w", err)
	}
	if err := temp.Close(); err != nil {
		return fmt.Errorf("close state: %w", err)
	}
	if err := os.Rename(tempName, s.path); err != nil {
		return fmt.Errorf("replace state: %w", err)
	}
	return nil
}

func cloneState(state persistedState) (persistedState, error) {
	data, err := json.Marshal(state)
	if err != nil {
		return persistedState{}, err
	}
	var next persistedState
	if err := json.Unmarshal(data, &next); err != nil {
		return persistedState{}, err
	}
	return next, nil
}

func randomCredential(bytes int) (string, error) {
	value := make([]byte, bytes)
	if _, err := rand.Read(value); err != nil {
		return "", fmt.Errorf("generate credential: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(value), nil
}

func hashCredential(raw string) string {
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

func cleanText(value string, max int) string {
	value = strings.TrimSpace(strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return -1
		}
		return r
	}, value))
	runes := []rune(value)
	if len(runes) > max {
		return string(runes[:max])
	}
	return value
}

func defaultDeviceLabel(metadata DeviceMetadata) string {
	return firstNonEmpty(
		cleanText(metadata.Model, 80),
		cleanText(metadata.Platform, 40),
		"新设备",
	)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}
