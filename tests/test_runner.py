import os
import sys
import tempfile
import time
import unittest

from tdforge.gui.runner import Job, Screen, tool_command


class RunnerTests(unittest.TestCase):
    def test_make_fixture_runs_like_the_cli(self):
        with tempfile.TemporaryDirectory() as d:
            job = Job(tool_command("make_fixture", ["--out-dir", d]))
            self.assertEqual(job.wait(60), 0)
            self.assertTrue(os.path.exists(os.path.join(d, "badge.3mf")))
            self.assertIn("triangles", job.drain())

    def test_argparse_error_comes_back_as_text_and_exit_code(self):
        job = Job(tool_command("stackforge", ["--nope"]))
        self.assertEqual(job.wait(60), 2)
        self.assertIn("error:", job.drain())

    def test_cancel(self):
        job = Job([sys.executable, "-u", "-c", "import time; print('go'); time.sleep(60)"])
        t0 = time.monotonic()
        while "go" not in (job.drain() or "") and time.monotonic() - t0 < 10:
            time.sleep(0.02)
        job.cancel()
        job.wait(10)
        self.assertTrue(job.cancelled)
        self.assertNotEqual(job.returncode, 0)

    def test_cancel_escalates_to_kill(self):
        import tdforge.gui.runner as r
        old, r.KILL_AFTER = r.KILL_AFTER, 0.3
        self.addCleanup(setattr, r, "KILL_AFTER", old)
        code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
        job = Job([sys.executable, "-u", "-c", code])
        t0 = time.monotonic()
        buf = ""
        while "ready" not in buf and time.monotonic() - t0 < 10:
            buf += job.drain()
            time.sleep(0.02)
        job.cancel()
        job.wait(10)
        self.assertLess(job.elapsed, 8)

    def test_input_prompts(self):
        """The munki shape: print a prompt, wait for Enter, carry on."""
        code = ("x = input('place chip, press Enter... ')\n"
                "print('got', repr(x))\n"
                "y = input('again? ')\n"
                "print('done', y)\n")
        job = Job([sys.executable, "-u", "-c", code])
        out = ""
        t0 = time.monotonic()
        while "chip" not in out and time.monotonic() - t0 < 10:
            out += job.drain()
            time.sleep(0.02)
        job.send("")
        while "again?" not in out and time.monotonic() - t0 < 10:
            out += job.drain()
            time.sleep(0.02)
        job.send("yes")
        job.wait(10)
        out += job.drain()
        self.assertIn("got ''", out)
        self.assertIn("done yes", out)
        self.assertEqual(job.returncode, 0)




class ScreenTests(unittest.TestCase):
    def test_ansi_cr_backspace(self):
        s = Screen()
        s.write("\x1b[31mred\x1b[0m\nprogress 10%\rprogress 90%\nab\bc\r\n")
        self.assertEqual(s.text, "red\nprogress 90%\nac\n")

    def test_chunks_split_mid_line(self):
        s = Screen()
        s.write("hel")
        s.write("lo\nwor")
        s.write("ld")
        self.assertEqual(s.text, "hello\nworld")


if __name__ == "__main__":
    unittest.main()
