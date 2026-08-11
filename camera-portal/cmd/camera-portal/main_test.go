package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestEnvOrFileReadsDockerSecretWithoutChangingLegacyEnv(t *testing.T) {
	t.Setenv("LOCAL_ACCESS_KEY", "legacy-value")
	t.Setenv("LOCAL_ACCESS_KEY_FILE", "")
	value, err := envOrFile("LOCAL_ACCESS_KEY")
	if err != nil || value != "legacy-value" {
		t.Fatalf("legacy env = %q, %v", value, err)
	}

	t.Setenv("LOCAL_ACCESS_KEY", "")
	secretPath := filepath.Join(t.TempDir(), "LAN_ACCESS_KEY")
	if err := os.WriteFile(secretPath, []byte("docker-secret-value\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("LOCAL_ACCESS_KEY_FILE", secretPath)
	value, err = envOrFile("LOCAL_ACCESS_KEY")
	if err != nil || value != "docker-secret-value" {
		t.Fatalf("secret file = %q, %v", value, err)
	}
}

func TestEnvOrFileFailsClosedForAmbiguousOrInvalidSecret(t *testing.T) {
	t.Setenv("LOCAL_ACCESS_KEY", "legacy-value")
	t.Setenv("LOCAL_ACCESS_KEY_FILE", filepath.Join(t.TempDir(), "secret"))
	if _, err := envOrFile("LOCAL_ACCESS_KEY"); err == nil || !strings.Contains(err.Error(), "both") {
		t.Fatalf("ambiguous secret error = %v", err)
	}

	t.Setenv("LOCAL_ACCESS_KEY", "")
	t.Setenv("LOCAL_ACCESS_KEY_FILE", filepath.Join(t.TempDir(), "missing"))
	if _, err := envOrFile("LOCAL_ACCESS_KEY"); err == nil {
		t.Fatal("missing secret file was accepted")
	}

	largePath := filepath.Join(t.TempDir(), "large")
	if err := os.WriteFile(largePath, make([]byte, 4097), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("LOCAL_ACCESS_KEY_FILE", largePath)
	if _, err := envOrFile("LOCAL_ACCESS_KEY"); err == nil || !strings.Contains(err.Error(), "too large") {
		t.Fatalf("large secret error = %v", err)
	}
}
