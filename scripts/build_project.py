#!/usr/bin/env python3
"""AndroidForge — Build Project.

Reads detection + setup JSON and runs the appropriate build command(s).
Supports Flutter APK, Flutter AAB, and Gradle build variants.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def run(
    cmd: list[str],
    cwd: Path,
    log_file: Path,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a command and stream output to console and log."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n=== Running: {' '.join(cmd)}", flush=True)
    print(f"    cwd: {cwd}", flush=True)
    print(f"    log: {log_file}", flush=True)

    start = time.time()
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    with log_file.open("w", encoding="utf-8", errors="replace") as f:
        f.write(f"$ {' '.join(cmd)}\n")
        f.write(f"# cwd: {cwd}\n\n")
        f.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            f.write(line)

        rc = proc.wait()

    elapsed = time.time() - start
    print(f"=== Exit {rc} after {elapsed:.1f}s", flush=True)
    return rc, str(log_file)


def maybe_regenerate_wrapper(
    project_root: Path,
    gradle_version: str | None,
    log_dir: Path,
) -> bool:
    """Regenerate Gradle wrapper if its JAR is missing."""
    jar = project_root / "gradle" / "wrapper" / "gradle-wrapper.jar"

    if jar.exists():
        return True

    if not gradle_version:
        print(
            "WARNING: cannot regenerate wrapper — "
            "no Gradle version specified",
            flush=True,
        )
        return False

    cmd = [
        "gradle",
        "wrapper",
        "--gradle-version",
        gradle_version,
        "--distribution-type",
        "bin",
    ]

    rc, _ = run(cmd, project_root, log_dir / "regenerate-wrapper.log")
    return rc == 0 and jar.exists()


def filter_build_commands(
    commands: list[list[str]],
    variant: str,
) -> list[list[str]]:
    """Select commands matching the requested build variant."""
    if variant == "auto":
        return commands

    filtered: list[list[str]] = []

    for cmd in commands:
        cmd_str = " ".join(cmd).lower()

        if variant == "debug" and "debug" in cmd_str:
            filtered.append(cmd)
        elif variant == "release" and "release" in cmd_str:
            filtered.append(cmd)
        elif variant == "bundle" and (
            "bundle" in cmd_str or "appbundle" in cmd_str
        ):
            filtered.append(cmd)

    return filtered


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AndroidForge build project"
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--detect", required=True)
    parser.add_argument("--toolchain", required=True)
    parser.add_argument(
        "--variant",
        default="auto",
        choices=["auto", "debug", "release", "bundle"],
    )
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    def load(value: str) -> Any:
        path = Path(value)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return json.loads(value)

    detect = load(args.detect)
    toolchain = load(args.toolchain)

    root = Path(args.root).resolve()
    log_dir = (
        Path(args.log_dir).resolve()
        if args.log_dir
        else root.parent / "androidforge-logs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    project_type = detect.get("project_type", "unknown")
    needs_gradle = toolchain.get("needs_gradle", False)
    needs_flutter = toolchain.get("needs_flutter", False)
    use_wrapper = toolchain.get("use_wrapper", False)

    all_commands = toolchain.get("build_commands", [])
    prep_commands = toolchain.get("prep_commands", []) or []

    # Flutter AAB must use appbundle, not build apk.
    if needs_flutter and args.variant == "bundle":
        all_commands = [
            ["flutter", "build", "appbundle", "--release"]
        ]
    else:
        all_commands = filter_build_commands(
            all_commands,
            args.variant,
        )

    # Do not silently build an APK when a bundle was requested.
    if args.variant == "bundle" and not all_commands:
        message = (
            "ERROR: No AAB/bundle build command was found "
            "for the requested variant."
        )
        print(message, file=sys.stderr, flush=True)
        return 1

    # Regenerate Gradle wrapper if needed.
    if use_wrapper and project_type != "flutter":
        gradlew = root / "gradlew"
        if not gradlew.exists():
            print(
                "WARNING: gradlew missing — trying to regenerate it",
                flush=True,
            )
            if not maybe_regenerate_wrapper(
                root,
                toolchain.get("gradle_version"),
                log_dir,
            ):
                print(
                    "ERROR: Gradle wrapper regeneration failed.",
                    file=sys.stderr,
                    flush=True,
                )
                return 1

    build_env: dict[str, str] = {}

    # Run preparation commands.
    if prep_commands:
        print(
            f"\n=== Running {len(prep_commands)} prep command(s)",
            flush=True,
        )

    for index, original_cmd in enumerate(prep_commands):
        cmd = list(original_cmd)

        if use_wrapper and cmd and cmd[0].endswith("gradlew"):
            gradlew_path = Path(cmd[0])
            if gradlew_path.exists():
                try:
                    gradlew_path.chmod(
                        gradlew_path.stat().st_mode | 0o111
                    )
                except OSError:
                    pass

                cmd = ["/bin/sh", str(gradlew_path)] + cmd[1:]

        rc, _ = run(
            cmd,
            root,
            log_dir / f"prep-{int(time.time())}-{index}.log",
            build_env,
        )

        if rc != 0:
            print(
                f"WARNING: prep command failed: {' '.join(cmd)}",
                flush=True,
            )
        else:
            print(
                f"Prep command succeeded: {' '.join(cmd)}",
                flush=True,
            )

    # Run build commands, stopping after the first success.
    success = False
    last_rc = 1
    last_log = ""
    successful_command: list[str] | None = None

    for index, original_cmd in enumerate(all_commands):
        cmd = list(original_cmd)

        if use_wrapper and cmd and cmd[0].endswith("gradlew"):
            gradlew_path = Path(cmd[0])

            if gradlew_path.exists():
                try:
                    gradlew_path.chmod(
                        gradlew_path.stat().st_mode | 0o111
                    )
                except OSError:
                    pass

                cmd = ["/bin/sh", str(gradlew_path)] + cmd[1:]

        rc, log = run(
            cmd,
            root,
            log_dir / f"build-{int(time.time())}-{index}.log",
            build_env,
        )

        last_rc = rc
        last_log = log

        if rc == 0:
            success = True
            successful_command = cmd
            break

    result = {
        "project_root": str(root),
        "project_type": project_type,
        "needs_gradle": needs_gradle,
        "build_succeeded": success,
        "requested_variant": args.variant,
        "successful_command": successful_command,
        "exit_code": last_rc,
        "log_file": last_log,
        "prep_commands_run": prep_commands,
        "build_commands_attempted": all_commands,
    }

    output = json.dumps(result, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)

    gh_output = os.environ.get("GITHUB_OUTPUT")

    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(
                f"build_succeeded="
                f"{'true' if success else 'false'}\n"
            )
            f.write(f"exit_code={last_rc}\n")
            f.write(f"log_file={last_log}\n")

            if successful_command:
                f.write(
                    "successful_command="
                    f"{' '.join(successful_command)}\n"
                )

            f.write(f"json<<EOF\n{json.dumps(result)}\nEOF\n")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
