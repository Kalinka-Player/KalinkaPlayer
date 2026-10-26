from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]


class RaspberryPiSupervisorIntegrationTest(unittest.TestCase):
    def test_rpi_image_gen_runs_pi_setup_and_checks_before_verification(self):
        supervisor = (ROOT / "packages/kalinka-image/lib/supervisor.sh").read_text()
        hook = (ROOT / "packages/kalinka-image/rpi-image-gen/kalinka-hook.sh").read_text()

        install = supervisor.split("install_supervisor() {", 1)[1].split("\n}", 1)[0]
        verify = supervisor.split("verify_supervisor() {", 1)[1].split("\n}", 1)[0]
        self.assertIn(
            'configure_supervisor_radios "$ROOTFS"\n'
            '  in_chroot systemctl enable hciuart.service\n'
            '  in_chroot systemctl disable serial-getty@ttyAMA0.service\n'
            '  if [ "${DIETPI_LAYOUT:-}" = rpi ]; then\n'
            '    # The unit is only useful on DietPi, where dietpi-set_hardware exists.\n'
            '    in_chroot systemctl enable kalinka-soundcard.service\n'
            '  fi\n'
            '  configure_soundcard_detection "$ROOTFS"',
            install,
        )
        self.assertNotIn("dietpi-disable_bluetooth.conf", verify)
        self.assertNotIn("dietpi-disable_wifi.conf", verify)
        self.assertNotIn("kalinka-soundcard.service", verify)
        self.assertIn('configure_soundcard_detection "$ROOTFS"', install)
        self.assertLess(hook.index("install_supervisor"), hook.index("verify_supervisor"))
        self.assertNotIn("printf 'i2c-dev\\n'", hook)


if __name__ == "__main__":
    unittest.main()
