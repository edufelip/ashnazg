import tempfile
import unittest
from pathlib import Path

from runtime.microvm.kernel_config import ConfigValidationError, validate_config_text


class KernelConfigValidationTests(unittest.TestCase):
    def assert_invalid(self, fragment, generated, message):
        with self.assertRaisesRegex(ConfigValidationError, message):
            validate_config_text(fragment, generated)

    def test_rejects_bridge_downgrade_from_y_to_m(self):
        self.assert_invalid(
            "CONFIG_IPV6=y\nCONFIG_BRIDGE=y\n",
            "CONFIG_IPV6=m\nCONFIG_BRIDGE=m\n",
            r"CONFIG_IPV6.*requested y.*generated m",
        )

    def test_rejects_requested_symbol_missing_from_generated_config(self):
        self.assert_invalid("CONFIG_BRIDGE=y\n", "CONFIG_IPV6=y\n", r"CONFIG_BRIDGE.*missing")

    def test_n_assignment_matches_kernel_disabled_comment(self):
        validate_config_text("CONFIG_DEBUG_INFO=n\n", "# CONFIG_DEBUG_INFO is not set\n")

    def test_disabled_fragment_comment_is_an_explicit_request(self):
        self.assert_invalid('# CONFIG_DEBUG_INFO is not set\n', 'CONFIG_DEBUG_INFO=y\n', r'CONFIG_DEBUG_INFO.*requested n.*generated y')
        validate_config_text('# CONFIG_DEBUG_INFO is not set\n', '# CONFIG_DEBUG_INFO is not set\n')

    def test_n_assignment_does_not_treat_absent_symbol_as_disabled(self):
        self.assert_invalid("CONFIG_DEBUG_INFO=n\n", "CONFIG_IPV6=y\n", r"CONFIG_DEBUG_INFO.*missing")

    def test_compares_quoted_string_and_numeric_values_exactly(self):
        validate_config_text(
            'CONFIG_ANDROID_BINDER_DEVICES="binder,hwbinder,vndbinder"\nCONFIG_HZ=250\n',
            'CONFIG_ANDROID_BINDER_DEVICES="binder,hwbinder,vndbinder"\nCONFIG_HZ=250\n',
        )
        self.assert_invalid('CONFIG_HZ=250\n', 'CONFIG_HZ=100\n', r"CONFIG_HZ.*requested 250.*generated 100")

    def test_rejects_malformed_fragment_assignment_with_line_number(self):
        self.assert_invalid("CONFIG_IPV6=y\nCONFIG_BRIDGE = y\n", "CONFIG_IPV6=y\n", r"fragment:2.*malformed")

    def test_rejects_duplicate_fragment_symbols(self):
        self.assert_invalid("CONFIG_IPV6=y\nCONFIG_IPV6=m\n", "CONFIG_IPV6=y\n", r"fragment:2.*duplicate.*CONFIG_IPV6")

    def test_accepts_matching_values_and_kernel_disabled_form(self):
        validate_config_text(
            'CONFIG_IPV6=y\nCONFIG_BRIDGE=y\nCONFIG_MT792x_LIB=m\nCONFIG_LOCALVERSION=""\nCONFIG_LOCALVERSION_AUTO=n\n',
            'CONFIG_IPV6=y\nCONFIG_BRIDGE=y\nCONFIG_MT792x_LIB=m\nCONFIG_LOCALVERSION=""\n# CONFIG_LOCALVERSION_AUTO is not set\n',
        )

    def test_cli_returns_failure_and_reports_downgrade(self):
        from runtime.microvm import kernel_config
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as directory:
            fragment = Path(directory) / "kernel.config"
            generated = Path(directory) / ".config"
            fragment.write_text("CONFIG_BRIDGE=y\n", encoding="utf-8")
            generated.write_text("CONFIG_BRIDGE=m\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(Path(kernel_config.__file__)), "--fragment", str(fragment), "--generated", str(generated)],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFIG_BRIDGE", result.stderr)


if __name__ == "__main__":
    unittest.main()
