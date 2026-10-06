package wifi

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"

	"kalinka/supervisor/internal/protocol"
)

// AtomicWrite makes configuration durable before acknowledging a join.
func AtomicWrite(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return protocol.Storage
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".kalinka-*")
	if err != nil {
		return protocol.Storage
	}
	defer os.Remove(f.Name())
	ok := false
	defer func() {
		if !ok {
			_ = f.Close()
		}
	}()
	if f.Chmod(mode) != nil {
		return protocol.Storage
	}
	if _, err = f.Write(data); err != nil {
		return protocol.Storage
	}
	if f.Sync() != nil {
		return protocol.Storage
	}
	if f.Close() != nil {
		return protocol.Storage
	}
	ok = true
	if os.Rename(f.Name(), path) != nil {
		return protocol.Storage
	}
	return syncDir(filepath.Dir(path))
}
func syncDir(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return protocol.Storage
	}
	defer f.Close()
	if f.Sync() != nil {
		return protocol.Storage
	}
	return nil
}
func removeDurable(path string) error {
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return protocol.Storage
	}
	return syncDir(filepath.Dir(path))
}
func readOptional(path string) ([]byte, error) {
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, protocol.Storage
	}
	return b, nil
}

// Transaction stores a fixed list of configuration files for interrupted joins.
type Transaction struct {
	Dir   string
	Paths []string
}

func (t Transaction) Pending() bool { _, err := os.Stat(t.Dir); return err == nil }
func (t Transaction) Begin() error {
	if t.Pending() {
		return protocol.Busy
	}
	if os.MkdirAll(filepath.Dir(t.Dir), 0700) != nil {
		return protocol.Storage
	}
	temp, err := os.MkdirTemp(filepath.Dir(t.Dir), "rollback-")
	if err != nil {
		return protocol.Storage
	}
	defer os.RemoveAll(temp)
	for i, path := range t.Paths {
		info, err := os.Stat(path)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return protocol.Storage
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return protocol.Storage
		}
		if err = AtomicWrite(filepath.Join(temp, strconv.Itoa(i)), b, 0600); err != nil {
			return err
		}
		if err = AtomicWrite(filepath.Join(temp, strconv.Itoa(i)+".mode"), []byte(strconv.Itoa(int(info.Mode().Perm()))), 0600); err != nil {
			return err
		}
	}
	if os.Rename(temp, t.Dir) != nil {
		return protocol.Storage
	}
	return syncDir(filepath.Dir(t.Dir))
}
func (t Transaction) Restore() error {
	if !t.Pending() {
		return nil
	}
	for i, path := range t.Paths {
		b, err := os.ReadFile(filepath.Join(t.Dir, strconv.Itoa(i)))
		if os.IsNotExist(err) {
			if err = os.Remove(path); err != nil && !os.IsNotExist(err) {
				return protocol.Storage
			}
			continue
		}
		if err != nil {
			return protocol.Storage
		}
		mode := 0600
		if b, err := os.ReadFile(filepath.Join(t.Dir, strconv.Itoa(i)+".mode")); err == nil {
			if n, e := strconv.Atoi(string(b)); e == nil {
				mode = n & 0777
			}
		}
		if err = AtomicWrite(path, b, os.FileMode(mode)); err != nil {
			return err
		}
	}
	return t.Commit()
}
func (t Transaction) Commit() error {
	if !t.Pending() {
		return nil
	}
	retired := t.Dir + "-complete"
	if os.RemoveAll(retired) != nil {
		return protocol.Storage
	}
	if os.Rename(t.Dir, retired) != nil {
		return protocol.Storage
	}
	if err := syncDir(filepath.Dir(t.Dir)); err != nil {
		return err
	}
	// The durable rename is the commit point; cleanup can be retried later.
	_ = os.RemoveAll(retired)
	return nil
}
func writeJSON(path string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return protocol.Storage
	}
	return AtomicWrite(path, b, 0600)
}
