package wifi

import (
	"encoding/hex"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"kalinka/supervisor/internal/protocol"
)

var slotPattern = regexp.MustCompile(`^\s*aWIFI_SSID\[([0-4])\]=(.*)$`)

func shellWord(s string) (string, error) {
	var b strings.Builder
	var quote rune
	started, ended := false, false
	chars := []rune(s)
	for i := 0; i < len(chars); i++ {
		r := chars[i]
		if quote == '\'' {
			if r == '\'' {
				quote = 0
			} else {
				b.WriteRune(r)
			}
			continue
		}
		if quote == '"' {
			if r == '"' {
				quote = 0
				continue
			}
			if r == '\\' {
				i++
				if i == len(chars) {
					return "", protocol.Storage
				}
				if chars[i] != '"' && chars[i] != '\\' {
					b.WriteRune('\\')
				}
				b.WriteRune(chars[i])
			} else {
				b.WriteRune(r)
			}
			continue
		}
		if r == '#' {
			break
		}
		if unicode.IsSpace(r) {
			ended = started
			continue
		}
		if ended {
			return "", protocol.Storage
		}
		started = true
		switch r {
		case '\'', '"':
			quote = r
		case '\\':
			i++
			if i == len(chars) {
				return "", protocol.Storage
			}
			b.WriteRune(chars[i])
		default:
			b.WriteRune(r)
		}
	}
	if quote != 0 {
		return "", protocol.Storage
	}
	return b.String(), nil
}
func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }
func updateDatabase(contents, ssid, key string) (string, error) {
	slots := map[int]string{}
	lines := strings.Split(strings.TrimSuffix(contents, "\n"), "\n")
	for _, line := range lines {
		if m := slotPattern.FindStringSubmatch(line); m != nil {
			i, _ := strconv.Atoi(m[1])
			s, err := shellWord(m[2])
			if err != nil {
				return "", err
			}
			slots[i] = s
		}
	}
	index := -1
	for i := 0; i < 5; i++ {
		if slots[i] == ssid {
			index = i
			break
		}
	}
	if index < 0 {
		for i := 0; i < 5; i++ {
			if slots[i] == "" {
				index = i
				break
			}
		}
	}
	if index < 0 {
		return "", protocol.Storage
	}
	remove := regexp.MustCompile(`^\s*aWIFI_[A-Z0-9_]+\[` + strconv.Itoa(index) + `\]=`)
	kept := []string{}
	for _, line := range lines {
		if !remove.MatchString(line) && !(len(lines) == 1 && line == "") {
			kept = append(kept, line)
		}
	}
	prefix := "[" + strconv.Itoa(index) + "]="
	kept = append(kept, "aWIFI_SSID"+prefix+shellQuote(ssid), "aWIFI_KEY"+prefix+shellQuote(key), "aWIFI_KEYMGR"+prefix+"'WPA-PSK'")
	return strings.Join(kept, "\n") + "\n", nil
}

var networkBlock = regexp.MustCompile(`(?ms)^[ \t]*network=\{[ \t]*\n.*?^[ \t]*\}[ \t]*$`)
var networkStart = regexp.MustCompile(`(?m)^[ \t]*network[ \t]*=`)
var disabledLine = regexp.MustCompile(`(?m)^[ \t]*disabled=(\d+)[ \t]*(?:#.*)?$`)

func stageNetworks(contents string) (string, []string, error) {
	var states []string
	staged := networkBlock.ReplaceAllStringFunc(contents, func(block string) string {
		m := disabledLine.FindStringSubmatch(block)
		if m != nil {
			states = append(states, m[1])
			return disabledLine.ReplaceAllString(block, "\tdisabled=1")
		}
		states = append(states, "0")
		i := strings.LastIndex(block, "}")
		return block[:i] + "\tdisabled=1\n}"
	})
	if len(states) != len(networkStart.FindAllString(contents, -1)) {
		return "", nil, protocol.Storage
	}
	return staged, states, nil
}
func replaceSetting(contents, key, value string) string {
	r := regexp.MustCompile(`(?m)^[ \t]*` + regexp.QuoteMeta(key) + `=.*$`)
	if r.MatchString(contents) {
		return r.ReplaceAllStringFunc(contents, func(string) string { return key + "=" + value })
	}
	return contents + "\n" + key + "=" + value + "\n"
}

var bssPattern = regexp.MustCompile(`(?m)^BSS `)
var ssidPattern = regexp.MustCompile(`(?m)^\tSSID: (.*)$`)
var signalPattern = regexp.MustCompile(`(?m)^\tsignal: (-?\d+)(?:\.\S+)? dBm$`)

func ParseScan(output string) []protocol.Network {
	var result []protocol.Network
	for _, block := range bssPattern.Split(output, -1)[1:] {
		m := ssidPattern.FindStringSubmatch(block)
		signal := signalPattern.FindStringSubmatch(block)
		if m == nil || signal == nil {
			continue
		}
		s := m[1]
		var raw []byte
		valid := true
		for len(s) > 0 {
			if strings.HasPrefix(s, `\x`) {
				if len(s) < 4 {
					valid = false
					break
				}
				b, err := hex.DecodeString(s[2:4])
				if err != nil {
					valid = false
					break
				}
				raw = append(raw, b...)
				s = s[4:]
			} else {
				raw = append(raw, s[0])
				s = s[1:]
			}
		}
		if !valid || !protocol.ValidSSID(string(raw)) {
			continue
		}
		security := "open"
		rsn := false
		psk := false
		inRSN := false
		for _, line := range strings.Split(block, "\n") {
			if strings.HasPrefix(line, "\tRSN:") {
				rsn = true
				inRSN = true
			} else if strings.HasPrefix(line, "\t") && !strings.HasPrefix(line, "\t\t") && !strings.HasPrefix(line, "\t ") {
				inRSN = false
			}
			if inRSN && strings.Contains(line, "Authentication suites:") {
				parts := strings.SplitN(line, "Authentication suites:", 2)
				for _, suite := range strings.Fields(parts[1]) {
					if suite == "PSK" {
						psk = true
					}
				}
			}
			if strings.HasPrefix(line, "\tWPA:") || strings.HasPrefix(line, "\tcapability:") && strings.Contains(line, "Privacy") {
				security = "unsupported"
			}
		}
		if psk {
			security = "wpa2"
		} else if rsn {
			security = "unsupported"
		}
		strength, _ := strconv.Atoi(signal[1])
		result = append(result, protocol.Network{SSID: string(raw), Signal: max(-127, min(0, strength)), Security: security})
	}
	return strongest(result)
}
