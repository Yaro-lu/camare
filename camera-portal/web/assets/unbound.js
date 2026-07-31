fetch("/api/config", {
  credentials: "same-origin",
  cache: "no-store",
}).then((response) => {
  if (response.ok) {
    location.reload();
  }
}).catch(() => {
  // The regular unbound page remains visible when the network is unavailable.
});
