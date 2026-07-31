package webassets

import "embed"

// FS contains the complete, self-hosted portal interface.
//
//go:embed *.html assets/*
var FS embed.FS
