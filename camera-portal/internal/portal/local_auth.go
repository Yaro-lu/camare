package portal

import (
	"crypto/subtle"
	"net/http"
)

const localAccessHeader = "X-Camera-LAN-Key"

func (a *App) localEnabled() bool {
	return a.localOrigin != "" && a.localAccessKey != ""
}

func (a *App) isLocalRequest(r *http.Request) bool {
	if !a.localEnabled() {
		return false
	}
	if !sameHost(r.Host, a.localHost) {
		return false
	}
	got := []byte(r.Header.Get(localAccessHeader))
	want := []byte(a.localAccessKey)
	return len(got) == len(want) && subtle.ConstantTimeCompare(got, want) == 1
}
