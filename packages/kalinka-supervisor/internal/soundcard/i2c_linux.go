package soundcard

import (
	"unsafe"

	"golang.org/x/sys/unix"
)

// From linux/i2c-dev.h and linux/i2c.h, which golang.org/x/sys does not carry.
const (
	i2cSlaveForce    = 0x0706
	i2cSMBus         = 0x0720
	i2cSMBusRead     = 1
	i2cSMBusByteData = 2
)

// smbusIoctlData is struct i2c_smbus_ioctl_data.
type smbusIoctlData struct {
	readWrite uint8
	command   uint8
	size      uint32
	data      *[34]byte
}

type linuxBus struct{ fd int }

// HATBus is the I2C bus on the Pi's GPIO header, present once i2c_arm is on and i2c-dev is loaded.
const HATBus = "/dev/i2c-1"

// OpenI2C opens HATBus.
func OpenI2C() (Bus, error) {
	fd, err := unix.Open(HATBus, unix.O_RDWR|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	return &linuxBus{fd}, nil
}

// ReadRegister addresses the chip even while a driver holds it, as i2cget -f does; a single read leaves it as it was.
func (b *linuxBus) ReadRegister(addr, reg uint8) (uint8, error) {
	if err := unix.IoctlSetInt(b.fd, i2cSlaveForce, int(addr)); err != nil {
		return 0, err
	}
	data := new([34]byte)
	args := smbusIoctlData{readWrite: i2cSMBusRead, command: reg, size: i2cSMBusByteData, data: data}
	if _, _, errno := unix.Syscall(unix.SYS_IOCTL, uintptr(b.fd), i2cSMBus, uintptr(unsafe.Pointer(&args))); errno != 0 {
		return 0, errno
	}
	return data[0], nil
}

func (b *linuxBus) Close() error { return unix.Close(b.fd) }
