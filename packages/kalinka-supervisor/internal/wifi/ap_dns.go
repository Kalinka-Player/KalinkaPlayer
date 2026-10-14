package wifi

import (
	"context"
	"fmt"
	"path/filepath"
)

func startAPDNS(ctx context.Context, iface, runtime string, start func(context.Context, string, ...string) (APProcess, error)) (APProcess, error) {
	config := fmt.Sprintf("interface=%s\nexcept-interface=lo\nbind-interfaces\nlisten-address=%s\nno-resolv\nno-hosts\naddress=/#/%s\nlocal=/#/\ndhcp-range=192.168.4.10,192.168.4.100,255.255.255.0,10m\ndhcp-option=3,%s\ndhcp-option=6,%s\ndhcp-leasefile=%s\npid-file=\nuser=root\n", iface, APAddress, APAddress, APAddress, APAddress, filepath.Join(runtime, "hotspot.leases"))
	path := filepath.Join(runtime, "hotspot-dnsmasq.conf")
	if err := AtomicWrite(path, []byte(config), 0600); err != nil {
		return nil, err
	}
	if start == nil {
		start = startAPProcess
	}
	return start(ctx, "dnsmasq", "--keep-in-foreground", "--conf-file="+path)
}
