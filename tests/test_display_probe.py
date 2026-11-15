import pathlib
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
JAVA_SOURCE = ROOT / "runtime/display-governor/probe/src/com/ashnazg/displayprobe"


def compile_probe(classes):
    javac = shutil.which("javac")
    if not javac:
        raise unittest.SkipTest("javac is unavailable")
    sources = sorted(str(path) for path in JAVA_SOURCE.glob("*.java"))
    if not sources:
        raise AssertionError("probe Java sources are missing")
    subprocess.run([javac, "-d", str(classes), *sources], check=True)


def run_java(classes, main, *args):
    return subprocess.run(
        [shutil.which("java"), "-cp", str(classes), main, *args],
        text=True,
        capture_output=True,
    )


class DisplayProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.classes = pathlib.Path(self.temporary.name)
        compile_probe(self.classes)

    def tearDown(self):
        self.temporary.cleanup()

    def test_cli_defaults_to_read_only_inspection(self):
        result = run_java(self.classes, "com.ashnazg.displayprobe.DisplayProbe", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--inspect", result.stdout)
        self.assertIn("--qualify-modes --disposable-guest", result.stdout)

    def test_mode_qualification_requires_disposable_guest_acknowledgement(self):
        result = run_java(
            self.classes,
            "com.ashnazg.displayprobe.DisplayProbe",
            "--qualify-modes",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--disposable-guest", result.stderr)

    def test_argument_parser_rejects_unbounded_timeout_and_unknown_options(self):
        main = "com.ashnazg.displayprobe.DisplayProbe"
        qualify = ("--qualify-modes", "--disposable-guest")
        for args in ((*qualify, "--timeout-ms", "99"), (*qualify, "--timeout-ms", "5001"), ("--shell", "id")):
            with self.subTest(args=args):
                result = run_java(self.classes, main, *args)
                self.assertNotEqual(result.returncode, 0)

    def test_policy_accepts_only_unique_720x1280_one_and_sixty_hz_modes(self):
        harness = self.classes / "ProbePolicyTest.java"
        harness.write_text(
            """
import com.ashnazg.displayprobe.ProbePolicy;
import java.util.Arrays;
public class ProbePolicyTest {
    public static void main(String[] args) {
        ProbePolicy.Mode one = new ProbePolicy.Mode(1, 720, 1280, 1f);
        ProbePolicy.Mode sixty = new ProbePolicy.Mode(2, 720, 1280, 60f);
        ProbePolicy.validateModes(Arrays.asList(one, sixty));
        expectReject(Arrays.asList(one, sixty, new ProbePolicy.Mode(3, 720, 1280, 60f)));
        expectReject(Arrays.asList(one));
        expectReject(Arrays.asList(one, new ProbePolicy.Mode(2, 1080, 1920, 60f)));
    }
    private static void expectReject(java.util.List<ProbePolicy.Mode> modes) {
        try { ProbePolicy.validateModes(modes); throw new AssertionError("accepted unsafe modes"); }
        catch (IllegalArgumentException expected) { }
    }
}
""",
            encoding="utf-8",
        )
        subprocess.run(
            [shutil.which("javac"), "-cp", str(self.classes), "-d", str(self.classes), str(harness)],
            check=True,
        )
        result = run_java(self.classes, "ProbePolicyTest")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_absolute_deadline_expires_at_its_original_bound(self):
        harness = self.classes / "ProbeDeadlineTest.java"
        harness.write_text(
            """
import com.ashnazg.displayprobe.ProbePolicy;
public class ProbeDeadlineTest {
    public static void main(String[] args) {
        ProbePolicy.Deadline deadline = new ProbePolicy.Deadline(1000L, 200);
        if (deadline.remainingMillis(1000L) != 200L) throw new AssertionError("wrong initial bound");
        if (deadline.remainingMillis(100_000_000L) != 101L) throw new AssertionError("deadline moved");
        if (deadline.remainingMillis(201_000_000L) != 0L) throw new AssertionError("deadline did not expire");
        try { new ProbePolicy.Deadline(0L, 5001); throw new AssertionError("accepted unbounded wait"); }
        catch (IllegalArgumentException expected) { }
    }
}
""",
            encoding="utf-8",
        )
        subprocess.run(
            [shutil.which("javac"), "-cp", str(self.classes), "-d", str(self.classes), str(harness)],
            check=True,
        )
        result = run_java(self.classes, "ProbeDeadlineTest")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_script_rejects_missing_sdk_without_downloading(self):
        script = ROOT / "scripts/build_display_probe.sh"
        result = subprocess.run(
            ["env", "ANDROID_HOME=/missing", "ANDROID_SDK_ROOT=/missing", str(script), "--output", str(self.classes / "probe.jar")],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SDK platform 35", result.stderr)

    def test_build_script_produces_dex_jar_and_hash_provenance_when_sdk_is_available(self):
        sdk = pathlib.Path(
            os.environ.get("ANDROID_HOME")
            or os.environ.get("ANDROID_SDK_ROOT")
            or pathlib.Path.home() / "Library/Android/sdk"
        )
        if not (sdk / "platforms/android-35/android.jar").is_file():
            raise unittest.SkipTest("Android SDK platform 35 is unavailable")
        if not (sdk / "build-tools/35.0.0/d8").is_file():
            raise unittest.SkipTest("Android build-tools 35.0.0 are unavailable")
        output = self.classes / "display-probe.jar"
        result = subprocess.run(
            [str(ROOT / "scripts/build_display_probe.sh"), "--output", str(output)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        entries = subprocess.run(["jar", "tf", str(output)], text=True, capture_output=True, check=True)
        self.assertIn("classes.dex", entries.stdout.splitlines())
        provenance = output.with_suffix(".provenance.txt").read_text(encoding="utf-8")
        self.assertIn("platform_api_source_base_commit=cc8bb19595c661f5cf42f330457000e643c67d1b", provenance)
        self.assertIn("android_jar_sha256=", provenance)
        self.assertIn("d8_sha256=", provenance)
        self.assertIn("jar_sha256=", provenance)
