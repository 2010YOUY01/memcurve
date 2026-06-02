from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import matplotlib
import psutil

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402


BYTES_PER_MIB = 1024 * 1024
DEFAULT_FIGURE_TITLE = "CLI memory usage comparison"
AREA_COLORS = (
    "#00a6d6",
    "#e11d48",
    "#14b8a6",
    "#7c3aed",
    "#f59e0b",
    "#111827",
)


@dataclass(frozen=True)
class Sample:
    elapsed_s: float
    rss_mib: float


@dataclass(frozen=True)
class RunResult:
    index: int
    label: str
    command: str
    returncode: int
    duration_s: float
    peak_rss_mib: float
    samples: list[Sample]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run two or more CLI commands, sample process-tree RSS memory usage, "
            "and plot filled areas on one time axis."
        )
    )
    parser.add_argument(
        "--cmd",
        action="append",
        required=True,
        help="Command to execute. Repeat once per program, keeping shell quoting inside the value.",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Legend label for a command. Repeat in the same order as --cmd.",
    )
    parser.add_argument(
        "--output",
        default="memcurve.png",
        help="PNG output path for the overlapped plot. Default: %(default)s",
    )
    parser.add_argument(
        "--title",
        default=DEFAULT_FIGURE_TITLE,
        help="Figure title. Default: %(default)s",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="CSV sample output path. Default: next to --output using the same stem.",
    )
    parser.add_argument(
        "--summary",
        default=None,
        help="JSON summary output path. Default: next to --output using the same stem.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.01,
        help="Sampling interval in seconds. Default: %(default)s",
    )
    parser.add_argument(
        "--stdout",
        choices=("discard", "inherit", "log"),
        default="discard",
        help="How to handle command stdout. Default discards huge benchmark output.",
    )
    parser.add_argument(
        "--stderr",
        choices=("inherit", "discard", "log"),
        default="inherit",
        help="How to handle command stderr. Default: %(default)s",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Directory for --stdout log or --stderr log. Default: <output-stem>_logs.",
    )
    parser.add_argument(
        "--shell",
        default=os.environ.get("SHELL") or "/bin/sh",
        help="Shell used with --use-shell. Default: $SHELL or /bin/sh.",
    )
    parser.add_argument(
        "--use-shell",
        action="store_true",
        help="Run each command through --shell. By default commands are parsed and executed directly.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated plot with the macOS 'open' command after writing it.",
    )
    args = parser.parse_args(argv)

    if len(args.cmd) < 2:
        parser.error("provide at least two --cmd values to compare")
    if args.label and len(args.label) != len(args.cmd):
        parser.error("--label must be omitted or provided once for every --cmd")
    if args.interval <= 0:
        parser.error("--interval must be greater than zero")

    return args


def process_tree_rss_mib(root: psutil.Process) -> float:
    total_bytes = 0
    seen: set[int] = set()

    try:
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        processes = [root]

    for proc in processes:
        if proc.pid in seen:
            continue
        seen.add(proc.pid)
        try:
            total_bytes += proc.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return total_bytes / BYTES_PER_MIB


def safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._")
    return safe[:80] or "command"


def stream_target(
    mode: str,
    kind: str,
    log_dir: Path,
    label: str,
    opened_files: list[IO[bytes]],
) -> int | IO[bytes] | None:
    if mode == "inherit":
        return None
    if mode == "discard":
        return subprocess.DEVNULL

    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{safe_filename(label)}.{kind}.log"
    handle = path.open("wb")
    opened_files.append(handle)
    return handle


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_command(
    *,
    index: int,
    label: str,
    command: str,
    interval_s: float,
    shell: str,
    use_shell: bool,
    stdout_mode: str,
    stderr_mode: str,
    log_dir: Path,
) -> RunResult:
    print(f"[{index}] running: {label}", file=sys.stderr, flush=True)

    opened_files: list[IO[bytes]] = []
    stdout = stream_target(stdout_mode, "stdout", log_dir, label, opened_files)
    stderr = stream_target(stderr_mode, "stderr", log_dir, label, opened_files)

    start = time.monotonic()
    samples: list[Sample] = []
    process: subprocess.Popen[bytes] | None = None

    try:
        if use_shell:
            popen_args: str | list[str] = command
            popen_kwargs = {"shell": True, "executable": shell}
        else:
            popen_args = shlex.split(command)
            if not popen_args:
                raise ValueError("command is empty after parsing")
            popen_kwargs = {"shell": False}

        process = subprocess.Popen(
            popen_args,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            **popen_kwargs,
        )
        ps_process = psutil.Process(process.pid)

        while process.poll() is None:
            elapsed_s = time.monotonic() - start
            rss_mib = process_tree_rss_mib(ps_process)
            samples.append(Sample(elapsed_s=elapsed_s, rss_mib=rss_mib))
            time.sleep(interval_s)

        returncode = process.wait()
        duration_s = time.monotonic() - start

    except KeyboardInterrupt:
        if process is not None:
            terminate_process_group(process)
        raise
    finally:
        for handle in opened_files:
            handle.close()

    if not samples:
        samples.append(Sample(elapsed_s=0.0, rss_mib=0.0))

    peak_rss_mib = max(sample.rss_mib for sample in samples)
    print(
        f"[{index}] done: exit={returncode} duration={duration_s:.2f}s "
        f"peak={peak_rss_mib:.1f} MiB",
        file=sys.stderr,
        flush=True,
    )

    return RunResult(
        index=index,
        label=label,
        command=command,
        returncode=returncode,
        duration_s=duration_s,
        peak_rss_mib=peak_rss_mib,
        samples=samples,
    )


def plot_results(results: list[RunResult], output_path: Path, title: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(11, 6.5), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#fbfbfd")
    ax.set_axisbelow(True)

    for color_index, result in enumerate(results):
        color = AREA_COLORS[color_index % len(AREA_COLORS)]
        elapsed = [0.0, *[sample.elapsed_s for sample in result.samples]]
        memory = [result.samples[0].rss_mib, *[sample.rss_mib for sample in result.samples]]
        if len(elapsed) == 2:
            elapsed.append(max(result.duration_s, elapsed[-1] + 0.01))
            memory.append(memory[-1])

        label = (
            f"{result.label} "
            f"(peak {result.peak_rss_mib:.0f} MiB, {result.duration_s:.1f}s)"
        )

        ax.fill_between(
            elapsed,
            memory,
            step="post",
            color=color,
            alpha=0.58,
            linewidth=0,
            label=label,
        )
        ax.plot(
            elapsed,
            memory,
            drawstyle="steps-post",
            color=color,
            linewidth=1.8,
            alpha=0.95,
        )

    ax.set_title(title, fontsize=15, fontweight="semibold", pad=14)
    ax.set_xlabel("Elapsed time (seconds)", labelpad=9)
    ax.set_ylabel("Process tree RSS (MiB)", labelpad=9)
    ax.grid(True, which="major", color="#d4d7dd", linestyle="-", linewidth=0.7, alpha=0.65)
    ax.margins(x=0.01, y=0.08)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#c9ccd3")
    ax.tick_params(colors="#333947")
    ax.legend(
        loc="upper left",
        frameon=True,
        framealpha=0.92,
        facecolor="#ffffff",
        edgecolor="#d7dae0",
    )
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


def write_csv(results: list[RunResult], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["command_index", "label", "elapsed_s", "rss_mib"])
        for result in results:
            for sample in result.samples:
                writer.writerow(
                    [
                        result.index,
                        result.label,
                        f"{sample.elapsed_s:.6f}",
                        f"{sample.rss_mib:.6f}",
                    ]
                )


def write_summary(results: list[RunResult], summary_path: Path) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "command_index": result.index,
            "label": result.label,
            "command": result.command,
            "returncode": result.returncode,
            "duration_s": result.duration_s,
            "peak_rss_mib": result.peak_rss_mib,
            "sample_count": len(result.samples),
        }
        for result in results
    ]
    summary_path.write_text(json.dumps(payload, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    output_path = Path(args.output).expanduser().resolve()
    csv_path = (
        Path(args.csv).expanduser().resolve()
        if args.csv
        else output_path.with_suffix(".csv")
    )
    summary_path = (
        Path(args.summary).expanduser().resolve()
        if args.summary
        else output_path.with_suffix(".json")
    )
    log_dir = (
        Path(args.log_dir).expanduser().resolve()
        if args.log_dir
        else output_path.with_name(f"{output_path.stem}_logs")
    )

    labels = args.label or [f"command {idx}" for idx, _ in enumerate(args.cmd, start=1)]
    results: list[RunResult] = []

    for index, (label, command) in enumerate(zip(labels, args.cmd), start=1):
        results.append(
            run_command(
                index=index,
                label=label,
                command=command,
                interval_s=args.interval,
                shell=args.shell,
                use_shell=args.use_shell,
                stdout_mode=args.stdout,
                stderr_mode=args.stderr,
                log_dir=log_dir,
            )
        )

    plot_results(results, output_path, args.title)
    write_csv(results, csv_path)
    write_summary(results, summary_path)

    print(f"wrote plot: {output_path}", file=sys.stderr)
    print(f"wrote samples: {csv_path}", file=sys.stderr)
    print(f"wrote summary: {summary_path}", file=sys.stderr)

    if args.open:
        if sys.platform == "darwin":
            subprocess.run(["open", str(output_path)], check=False)
        else:
            print("--open is only supported on macOS", file=sys.stderr)

    return 0 if all(result.returncode == 0 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
