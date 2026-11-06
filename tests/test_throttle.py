import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "lean-throttle"
loader = importlib.machinery.SourceFileLoader("throttle", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
gate = importlib.util.module_from_spec(spec)
loader.exec_module(gate)


class ThrottleTests(unittest.TestCase):
    def test_spare_capacity_never_sleeps_even_with_cold_cpu_history(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, XDG_CACHE_HOME=directory),
            patch.object(gate, "cpu_sample", return_value=(100, 20)),
            patch.object(gate, "memory", return_value=(100 * 1024**3, 90 * 1024**3)),
            patch.object(gate.time, "sleep", side_effect=AssertionError("unconditional sleep")),
        ):
            gate.wait_for_capacity()
            gate.wait_for_capacity()

    def test_cpu_history_measures_across_invocations_and_reuses_recent_samples(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, XDG_CACHE_HOME=directory),
            patch.object(gate.time, "monotonic", side_effect=[100, 100.5, 100.6, 101, 120]),
            patch.object(gate, "cpu_sample", side_effect=[(100, 20), (200, 25), (210, 30), (300, 100), (400, 200)]),
            patch.object(gate.time, "sleep", side_effect=AssertionError("CPU sampling slept")),
        ):
            self.assertEqual(gate.cpu_usage(), 0)  # Cold start seeds counters.
            self.assertEqual(gate.cpu_usage(), 95)
            self.assertEqual(gate.cpu_usage(), 95)  # No noisy near-zero interval.
            self.assertEqual(gate.cpu_usage(), 25)
            self.assertEqual(gate.cpu_usage(), 0)  # Stale baseline starts afresh.

    def test_busy_cached_cpu_waits_then_resumes_after_pressure_clears(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, XDG_CACHE_HOME=directory),
            patch.object(gate.time, "monotonic", side_effect=[100, 100.5, 103]),
            patch.object(gate, "cpu_sample", side_effect=[(100, 20), (200, 25), (700, 525)]),
            patch.object(gate, "memory", return_value=(100 * 1024**3, 90 * 1024**3)),
            patch.object(gate.random, "uniform", return_value=2.5),
            patch.object(gate.time, "sleep") as sleep,
        ):
            gate.cpu_usage()
            gate.wait_for_capacity()
            sleep.assert_called_once_with(2.5)

    def test_bad_cpu_history_does_not_delay_or_fail_compilation(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, XDG_CACHE_HOME=directory),
            patch.object(gate, "cpu_sample", return_value=(100, 20)),
        ):
            cache = Path(directory) / "lean-throttle/cpu.json"
            cache.parent.mkdir()
            for text in ('', '{', 'null', '[]', '{"at": "bad"}',
                         '{"at": 0, "sample": null, "percent": 95}'):
                cache.write_text(text)
                self.assertEqual(gate.cpu_usage(), 0)


    def setUp(self):
        for name in ("SIGPIPE", "SIGXFSZ"):
            if number := getattr(signal, name, None):
                self.addCleanup(signal.signal, number, signal.getsignal(number))

    def test_waits_for_cpu_and_memory_then_resumes_silently(self):
        # Busy CPU, then low memory, then enough capacity at both thresholds.
        with (
            patch.object(gate, "cpu_usage", side_effect=[95, 80, 90]),
            patch.object(gate, "memory", side_effect=[(100, 50), (100, 5), (100, 20)]),
            patch.object(gate.random, "uniform", side_effect=[2, 4]),
            patch.object(gate.time, "sleep") as sleep,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            gate.wait_for_capacity()
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_cpu_ignores_guest_ticks_and_counts_iowait_as_idle(self):
        with patch.object(Path, "read_text", return_value="cpu 10 20 30 40 50 60 70 80 100 200\n"):
            self.assertEqual(gate.cpu_sample(), (360, 90))
        self.assertEqual(gate.cpu_percent((100, 10), (200, 20)), 90)
        self.assertEqual(gate.cpu_percent((100, 10), (200, 110)), 0)
        with self.assertRaises(ValueError):
            gate.cpu_percent((100, 10), (100, 10))

    def test_memory_bounds_host_by_ancestors_and_reclaims_only_clean_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proc/self").mkdir(parents=True)
            (root / "proc/meminfo").write_text("MemTotal: 67108864 kB\nMemAvailable: 50331648 kB\n")
            cgroup = root / "proc/self/cgroup"
            cgroup.write_text("0::/parent/child\n")
            cg = root / "sys/fs/cgroup"
            (cg / "parent/child").mkdir(parents=True)
            (cg / "memory.max").write_text("max\n")
            gib = 1024**3
            for name, limit, used in (("parent", 8, 7), ("parent/child", 6, 5)):
                group = cg / name
                (group / "memory.max").write_text(str(limit * gib))
                (group / "memory.current").write_text(str(used * gib))
                (group / "memory.stat").write_text(
                    f"file {3 * gib}\nshmem {gib}\nfile_dirty {gib // 4}\nfile_writeback {gib // 4}\n"
                )
                (group / "memory.high").write_text(str(gib))
            with patch.object(gate, "Path", side_effect=lambda path: root / str(path).lstrip("/")):
                self.assertEqual(gate.memory(), (6 * gib, 2.5 * gib))
            # The ancestor remains binding even when the child frees memory.
            (cg / "parent/child/memory.current").write_text(str(2 * gib))
            with patch.object(gate, "Path", side_effect=lambda path: root / str(path).lstrip("/")):
                self.assertEqual(gate.memory(), (6 * gib, 2.5 * gib))
            cgroup.write_text("1:memory:/legacy\n")
            with patch.object(gate, "Path", side_effect=lambda path: root / str(path).lstrip("/")):
                self.assertEqual(gate.memory(), (64 * gib, 48 * gib))

    def test_probes_skip_wait_and_compilation_preserves_arguments(self):
        with (
            patch.object(gate, "real_lean", return_value=Path("/real/lean")),
            patch.object(gate, "wait_for_capacity") as wait,
            patch.object(gate.os, "execv", side_effect=SystemExit(23)) as execute,
        ):
            for args in (["--githash"], ["--version"], ["-v"], ["--help"], ["-h"], ["--print-libdir"]):
                with self.assertRaises(SystemExit):
                    gate.main(args)
            wait.assert_not_called()
            args = ["File With Spaces.lean", "--setup", "config.json", "-o", "out.olean", "--json"]
            with self.assertRaises(SystemExit):
                gate.main(args)
            wait.assert_called_once()
            execute.assert_called_with("/real/lean", ["/real/lean", *args])
            self.assertEqual(signal.getsignal(signal.SIGPIPE), signal.SIG_DFL)

    def test_missing_telemetry_fails_before_compilation(self):
        with (
            patch.object(gate, "real_lean", return_value=Path("/real/lean")),
            patch.object(gate, "wait_for_capacity", side_effect=OSError("missing telemetry")),
            patch.object(gate.os, "execv") as execute,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(gate.main(["Main.lean"]), 2)
        execute.assert_not_called()
        self.assertIn("missing telemetry", stderr.getvalue())

    def test_overlay_is_concurrent_safe_persistent_and_preserves_compiler_status(self):
        with tempfile.TemporaryDirectory(prefix="lean throttle ") as directory:
            root = Path(directory)
            toolchain = root / "original"
            (toolchain / "bin").mkdir(parents=True)
            (toolchain / "lib").mkdir()
            lean = toolchain / "bin/lean"
            original = '#!/bin/sh\nprintf "%s\\n" "$@"\nexit 23\n'
            lean.write_text(original)
            lean.chmod(0o755)
            env = {
                **os.environ, "XDG_CACHE_HOME": str(root / "cache"),
                "LEAN_THROTTLE_REAL_LEAN": str(lean),
            }
            commands = [
                subprocess.Popen([str(SCRIPT), "--print-prefix"], env=env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(4)
            ]
            prefixes = []
            for command in commands:
                out, err = command.communicate(timeout=15)
                self.assertEqual(command.returncode, 0, err)
                self.assertEqual(err, "")
                prefixes.append(out.strip())
            self.assertEqual(len(set(prefixes)), 1)
            overlay = Path(prefixes[0])
            self.assertEqual((overlay / "lib").resolve(), toolchain / "lib")
            self.assertEqual((overlay / "bin/lean").read_bytes(), SCRIPT.read_bytes())
            self.assertEqual(lean.read_text(), original)
            # Saved metadata is sufficient even without the original setup env.
            for args, status, output in ((["--print-prefix"], 0, str(overlay)), (["--version"], 23, "--version")):
                result = subprocess.run([str(overlay / "bin/lean"), *args], env={"PATH": os.environ["PATH"]},
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stdout.strip(), output)

    def test_wrapper_cycles_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            gate.unwrapped(SCRIPT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            lean = root / "bin/lean"
            lean.write_bytes(SCRIPT.read_bytes())
            (root / gate.MARKER).write_text(json.dumps(dict(lean=str(lean))))
            with self.assertRaisesRegex(ValueError, "cycle"):
                gate.unwrapped(lean)

    def test_explicit_compiler_selection_precedes_toolchain_discovery(self):
        with (
            patch.dict(os.environ, LEAN_THROTTLE_REAL_LEAN="/custom/bin/lean"),
            patch.object(gate, "unwrapped", return_value=Path("/custom/bin/lean")) as unwrap,
        ):
            self.assertEqual(gate.real_lean(), Path("/custom/bin/lean"))
            unwrap.assert_called_once_with("/custom/bin/lean")


@unittest.skipUnless(os.environ.get("LEAN_THROTTLE_TEST_TOOLCHAIN"), "set LEAN_THROTTLE_TEST_TOOLCHAIN for a real Lake build")
class LakeIntegrationTests(unittest.TestCase):
    def test_real_lake_build_routes_compilation_through_the_wrapper(self):
        toolchain = Path(os.environ["LEAN_THROTTLE_TEST_TOOLCHAIN"]).resolve()
        with tempfile.TemporaryDirectory(prefix="lean throttle lake ") as directory:
            root = Path(directory)
            wrapper = root / "lean-throttle"
            log = root / "invocations.jsonl"
            # Log compiler admissions so this detects Lake silently bypassing us.
            # Deterministic admission avoids depending on the test host's load.
            source = SCRIPT.read_text().replace(
                "def wait_for_capacity():", "def wait_for_capacity():\n"
                f"    with open({str(log)!r}, 'a') as log:\n"
                "        log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "    return\n",
            )
            wrapper.write_text(source)
            wrapper.chmod(0o755)
            (root / "lakefile.toml").write_text('name = "smoke"\ndefaultTargets = ["Smoke"]\n[[lean_lib]]\nname = "Smoke"\n')
            (root / "Smoke").mkdir()
            (root / "Smoke/Leaf.lean").write_text("def leaf : Nat := 42\n")
            (root / "Smoke.lean").write_text("import Smoke.Leaf\nexample : leaf = 42 := rfl\n")
            env = {
                **os.environ, "LEAN": str(wrapper), "LAKE_OVERRIDE_LEAN": "true",
                "XDG_CACHE_HOME": str(root / "cache"),
            }
            env.pop("LEAN_SYSROOT", None)
            env.pop("LEAN_THROTTLE_REAL_LEAN", None)
            result = subprocess.run([str(toolchain / "bin/lake"), "build", "--iofail"],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((root / ".lake/build/lib/lean/Smoke.olean").is_file())
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            for module in ("Leaf.lean", "Smoke.lean"):
                self.assertTrue(any(any(arg.endswith(module) for arg in call) for call in calls), calls)
            # `lake env` must retain the overlay, including a nested Lake build.
            gate.shutil.rmtree(root / ".lake/build")
            nested = subprocess.run([str(toolchain / "bin/lake"), "env", "lake", "build", "--iofail"],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(nested.returncode, 0, nested.stdout + nested.stderr)
            self.assertGreater(len(log.read_text().splitlines()), len(calls))


if __name__ == "__main__":
    unittest.main()
