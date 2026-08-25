"""Regression tests for spot-interruption auto-recovery.

Covers the two halves of the fix that keeps a spot-reclaimed alignment chunk from failing a whole
sample (it should be retried on a fresh host instead):

  * scripts/init.sh handle_error re-surfaces a swallowed spot interruption as exit 143 when the
    reclamation marker is present, and leaves genuine tool errors untouched.
  * miniwdl-plugins/sfn_wdl _is_interruption classifies miniwdl's Terminated/"Interrupted" failure
    as an interruption (retryable) rather than a genuine pipeline error.

The init.sh test extracts and executes the REAL handle_error function out of scripts/init.sh (via a
balanced-brace slice), so it fails if the shipped logic regresses -- it does not re-implement it.
"""
import subprocess
import unittest
from os.path import dirname, realpath, join

REPO_ROOT = dirname(dirname(realpath(__file__)))
INIT_SH = join(REPO_ROOT, "scripts", "init.sh")


def _extract_shell_function(source: str, name: str) -> str:
    """Return the text of a `name() { ... }` shell function via brace balancing."""
    start = source.index(name + "() {")
    i = source.index("{", start)
    depth = 0
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[start:j + 1]
    raise AssertionError("unbalanced braces extracting %s" % name)


class TestInitShSpotRemap(unittest.TestCase):
    """Exercise the real scripts/init.sh handle_error exit-code remap."""

    @classmethod
    def setUpClass(cls):
        with open(INIT_SH) as fh:
            cls.handle_error = _extract_shell_function(fh.read(), "handle_error")
        # Sanity: we grabbed the function that contains the re-surfacing logic.
        assert "SPOT_INTERRUPTION_MARKER" in cls.handle_error

    def _run(self, simulated_exit_code, marker_present):
        """Set $? to simulated_exit_code, then run the real handle_error; return its exit code."""
        marker = "/tmp/swipe_selftest_marker"
        setup = "rm -f %s\n" % marker
        if marker_present:
            setup = "touch %s\n" % marker
        harness = (
            "set +e\n"
            'SPOT_INTERRUPTION_MARKER=%s\n' % marker
            + 'aws=true\n'
            + setup
            + self.handle_error
            + "\n(exit %d)\nhandle_error\n" % simulated_exit_code
        )
        # Run in an empty temp cwd so wdl_output.json does not exist and the jq branch is skipped.
        proc = subprocess.run(
            ["bash", "-c", harness],
            cwd="/tmp",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode

    def test_spot_interrupt_masked_exit2_is_resurfaced_as_143(self):
        # The bug: miniwdl swallows the spot SIGTERM into a clean exit 2. With the reclamation
        # marker set, handle_error must re-surface it as 143 so the retry rules fire.
        self.assertEqual(self._run(2, marker_present=True), 143)

    def test_genuine_error_without_marker_passes_through(self):
        # No interruption detected -> a real tool error keeps exit 2 and stays non-retryable.
        self.assertEqual(self._run(2, marker_present=False), 2)

    def test_sigterm_143_passes_through(self):
        self.assertEqual(self._run(143, marker_present=True), 143)

    def test_oom_137_is_not_reclassified(self):
        # A SIGKILL/OOM must stay 137 even if a stray marker exists (memory, not reclamation).
        self.assertEqual(self._run(137, marker_present=True), 137)


class TestPluginInterruptionClassification(unittest.TestCase):
    """_is_interruption() distinguishes a spot/host reclamation from a genuine failure."""

    def setUp(self):
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "sfnwdl_miniwdl_plugin",
                join(REPO_ROOT, "miniwdl-plugins", "sfn_wdl", "sfnwdl_miniwdl_plugin.py"),
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exn:  # miniwdl (WDL) not installed in this environment
            self.skipTest("sfnwdl_miniwdl_plugin unimportable: %s" % exn)
        self.mod = module

    def test_miniwdl_interrupted_message_is_interruption(self):
        exn = Exception("docker task running, exit code = -1 :: error: Interrupted")
        self.assertTrue(self.mod._is_interruption(exn))

    def test_terminated_exception_class_is_interruption(self):
        Terminated = type("Terminated", (Exception,), {})
        self.assertTrue(self.mod._is_interruption(Terminated("aborted")))

    def test_genuine_error_is_not_interruption(self):
        self.assertFalse(self.mod._is_interruption(ValueError("InvalidInputFileError: bad read")))


if __name__ == "__main__":
    unittest.main()
