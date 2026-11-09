package control

import (
	"io"
	"net/http"
	"os"
	"path"
	"strings"

	"kalinka/supervisor/internal/wifi"
)

// activationFallback stands in for the web build's page on a streamer without kalinka-web, or with one that predates it.
const activationFallback = `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="1;url=/?setup=network"><title>Set up Kalinka</title><body><h1>Set up your Kalinka streamer</h1><p>Opening Network setup…</p><p><a href="/?setup=network">Open Kalinka setup</a></p><p>If this window closes, open http://192.168.4.1 in your browser.</p></body></html>`

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
		name := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		// The file server would serve the page without its policy.
		if name == "activate.html" {
			http.Redirect(w, r, "/activate", http.StatusFound)
			return
		}
		if r.URL.Path == "/activate" {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Header().Set("Content-Security-Policy", pagePolicy)
			if page, err := os.Open(webDir + "/activate.html"); err == nil {
				defer page.Close()
				if info, err := page.Stat(); err == nil && !info.IsDir() {
					http.ServeContent(w, r, "", info.ModTime(), page)
					return
				}
			}
			io.WriteString(w, activationFallback)
			return
		}
		if r.URL.Path == "/" && r.URL.Query().Get("setup") != "network" {
			http.Redirect(w, r, "/?setup=network", http.StatusSeeOther)
			return
		}
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
