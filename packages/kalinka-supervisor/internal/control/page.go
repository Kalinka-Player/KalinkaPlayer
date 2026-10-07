package control

import (
	"embed"
	"net/http"
	"strconv"
)

//go:embed web
var web embed.FS

// asset is one file of the control page, built into the binary so the page
// works with Core, the network and the disk in any state.
type asset struct {
	file, contentType string
}

var assets = map[string]asset{
	"/":          {"web/index.html", "text/html; charset=utf-8"},
	"/app.js":    {"web/app.js", "text/javascript; charset=utf-8"},
	"/style.css": {"web/style.css", "text/css; charset=utf-8"},
	"/logo.svg":  {"web/logo.svg", "image/svg+xml"},
	"/icon.svg":  {"web/icon.svg", "image/svg+xml"},
}

func (a asset) serve(w http.ResponseWriter) string {
	b, err := web.ReadFile(a.file)
	if err != nil {
		return fail(w, "not_found")
	}
	w.Header().Set("Content-Type", a.contentType)
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	_, _ = w.Write(b)
	return ""
}
