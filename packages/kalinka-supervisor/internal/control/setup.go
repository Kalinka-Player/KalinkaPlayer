package control

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"

	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/rfkill"
	"kalinka/supervisor/internal/wifi"
)

// NetworkSetup shares one setup machine between HTTP and Bluetooth clients.
type NetworkSetup interface {
	Snapshot() map[string]any
	EnableWiFi(context.Context) error
	Open() error
	Command(string, []byte) error
}

func (h *handler) setupStatus(w http.ResponseWriter, r *http.Request) string {
	result := map[string]any{"available": false}
	if h.cfg.NetworkSetup != nil {
		result = h.cfg.NetworkSetup.Snapshot()
	}
	result["network"] = networkStatus(r.Context(), result["hotspot"] == true)
	reply(w, http.StatusOK, result, false)
	return ""
}

func networkStatus(ctx context.Context, hotspot bool) map[string]any {
	host := wifi.Host{Root: "/", Interfaces: net.Interfaces, Addrs: (*net.Interface).Addrs}
	return networkStatusOn(ctx, hotspot, host, wifi.Run)
}

func networkStatusOn(ctx context.Context, hotspot bool, host wifi.Host, run wifi.Runner) map[string]any {
	if hotspot {
		interfaces, _ := host.Interfaces()
		for _, iface := range interfaces {
			addrs, _ := host.Addrs(&iface)
			for _, addr := range addrs {
				ip, _, _ := net.ParseCIDR(addr.String())
				if ip.String() == wifi.APAddress {
					host.Exclude = iface.Name
				}
			}
		}
	}
	address, _ := host.Address(ctx, "")
	result := map[string]any{"connected": address != "", "address": address, "kind": "none"}
	interfaces, _ := host.Interfaces()
	for _, iface := range interfaces {
		addrs, _ := host.Addrs(&iface)
		for _, addr := range addrs {
			ip, _, _ := net.ParseCIDR(addr.String())
			if address == "" || ip.String() != address {
				continue
			}
			result["interface"] = iface.Name
			result["kind"] = "wired"
			if _, err := os.Stat(filepath.Join(host.Root, "sys/class/net", iface.Name, "phy80211")); err == nil {
				result["kind"] = "wifi"
				if ssid, err := wifi.ConnectedSSID(ctx, iface.Name, run); err == nil && ssid != "" {
					result["ssid"] = ssid
				}
			}
			return result
		}
	}
	return result
}

var setupClient = regexp.MustCompile(`^[a-zA-Z0-9-]{16,64}$`)

func (h *handler) setupCommand(w http.ResponseWriter, r *http.Request) string {
	if h.cfg.NetworkSetup == nil {
		return setupFailure(w, protocol.Unavailable)
	}
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		return fail(w, "unsupported_media_type")
	}
	client := r.Header.Get("X-Kalinka-Setup-Client")
	if !setupClient.MatchString(client) {
		return setupFailure(w, protocol.Invalid)
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, protocol.MaxMessage))
	if err != nil {
		return setupFailure(w, protocol.Invalid)
	}
	defer clear(raw)
	var command struct {
		Version int    `json:"v"`
		Op      string `json:"op"`
	}
	if json.Unmarshal(raw, &command) != nil || command.Version != 1 {
		return setupFailure(w, protocol.Invalid)
	}
	if command.Op == "enable_wifi" {
		err = h.cfg.NetworkSetup.EnableWiFi(r.Context())
	} else if command.Op == "open" {
		err = h.cfg.NetworkSetup.Open()
	} else {
		err = h.cfg.NetworkSetup.Command("http:"+client, raw)
	}
	if err != nil {
		return setupFailure(w, err)
	}
	reply(w, http.StatusAccepted, map[string]string{"status": "accepted"}, false)
	return "accepted"
}

func setupFailure(w http.ResponseWriter, err error) string {
	code, status := "unavailable", http.StatusServiceUnavailable
	if errors.Is(err, rfkill.ErrHardBlocked) {
		code, status = "radio_hard_blocked", http.StatusConflict
	}
	if err == protocol.Invalid {
		code, status = "invalid_request", http.StatusBadRequest
	}
	if err == protocol.Busy {
		code, status = "busy", http.StatusConflict
	}
	reply(w, status, map[string]string{"error": code}, false)
	return code
}
