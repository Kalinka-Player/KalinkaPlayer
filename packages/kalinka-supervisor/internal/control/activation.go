package control

import (
	"io"
	"net/http"
	"os"
	"path"
	"strings"

	"kalinka/supervisor/internal/wifi"
)

// NewSetupHandler serves the installed Kalinka web app even before Core is ready.
func NewSetupHandler(setup NetworkSetup, port func() int, webDir string) http.Handler {
	api := NewHandler(nil, HandlerConfig{NetworkSetup: setup, CorePort: port})
	files := http.FileServer(http.Dir(webDir))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Frame-Options", "DENY")
		if !remoteAllowed(r.RemoteAddr) {
			fail(w, "not_local")
			return
		}
		if r.URL.Path == "/v1/setup" {
			api.ServeHTTP(w, r)
			return
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			fail(w, "method_not_allowed")
			return
		}
		if hostname(r.Host) != wifi.APAddress || captiveProbe(r.URL.Path) {
			http.Redirect(w, r, "http://"+wifi.APAddress+"/activate", http.StatusFound)
			return
		}
		if r.URL.Path == "/activate" {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Header().Set("Content-Security-Policy", pagePolicy)
			io.WriteString(w, `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="1;url=/?setup=network"><title>Set up Kalinka</title><body><h1>Set up your Kalinka box</h1><p>Opening Network setup…</p><p><a href="/?setup=network">Open Kalinka setup</a></p><p>If this window closes, open http://192.168.4.1 in your browser.</p></body></html>`)
			return
		}
		if r.URL.Path == "/" && r.URL.Query().Get("setup") != "network" {
			http.Redirect(w, r, "/?setup=network", http.StatusSeeOther)
			return
		}
		name := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		if name == "" {
			name = "index.html"
		}
		info, err := os.Stat(webDir + "/" + name)
		if err != nil || info.IsDir() {
			if name == "index.html" {
				http.Error(w, "Install the kalinka-web package to use browser setup. Bluetooth setup is also available in the Kalinka app.", http.StatusServiceUnavailable)
			} else {
				http.NotFound(w, r)
			}
			return
		}
		files.ServeHTTP(w, r)
	})
}

func captiveProbe(p string) bool {
	switch p {
	case "/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html", "/connecttest.txt", "/ncsi.txt", "/redirect":
		return true
	}
	return false
}
