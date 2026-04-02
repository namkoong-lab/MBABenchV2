#!/usr/bin/env python3
"""
Claude Web Batch Runner - Run multiple tasks through Claude.ai sequentially.

This script orchestrates running multiple tasks through the Claude.ai or
ChatGPT web interface.

Usage:
    # Run tasks from a YAML file
    python claude_web_batch_runner.py --tasks tasks.yaml

    # With custom template
    python claude_web_batch_runner.py --tasks tasks.yaml --template template_claude_web.yaml

    # Dry run (show what would be executed)
    python claude_web_batch_runner.py --tasks tasks.yaml --dry-run

    # Run specific task indices
    python claude_web_batch_runner.py --tasks tasks.yaml --start 0 --end 5
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Global for signal handling
_current_process = None


def _signal_handler(signum, frame):
    """Handle Ctrl+C - terminate current task."""
    global _current_process
    if _current_process:
        logger.warning("Interrupt received - terminating current task...")
        _current_process.terminate()
        try:
            _current_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _current_process.kill()
    sys.exit(1)


class ClaudeWebBatchRunner:
    """
    Orchestrates running multiple tasks through Claude.ai or ChatGPT web interface.
    """

    def __init__(
        self,
        template_path: Path = None,
        engine_script: Path = None,
        python_cmd: list = None,
    ):
        """
        Initialize batch runner.

        Args:
            template_path: Path to template YAML config
            engine_script: Path to claude_web_engine.py
            python_cmd: Python command to use (default: ["python"])
        """
        self.template_path = template_path
        self.template = self._load_template() if template_path else {}
        self.provider = "claude"  # Default, overridden by CLI

        # Find engine script
        if engine_script:
            self.engine_script = Path(engine_script)
        else:
            self.engine_script = (
                Path(__file__).parent / "claude_web_agent" / "claude_web_engine.py"
            )

        if not self.engine_script.exists():
            raise FileNotFoundError(f"Engine script not found: {self.engine_script}")

        self.python_cmd = python_cmd or [sys.executable]

    def _load_template(self) -> dict:
        """Load template configuration."""
        with open(self.template_path, "r") as f:
            data = yaml.safe_load(f)
        return data.get("template", data)

    def _merge_config(self, task: dict) -> dict:
        """
        Merge task config with template.

        Args:
            task: Task-specific configuration

        Returns:
            Merged configuration dictionary
        """
        import copy

        # Start with template
        config = copy.deepcopy(self.template)

        # Override with task-specific values
        for key, value in task.items():
            if (
                isinstance(value, dict)
                and key in config
                and isinstance(config[key], dict)
            ):
                config[key].update(value)
            else:
                config[key] = value

        # Inject agent_type based on provider
        if hasattr(self, "provider"):
            provider_map = {"claude": "claude_web", "chatgpt": "chatgpt_web"}
            config["agent_type"] = provider_map.get(self.provider, "claude_web")

        return config

    def load_tasks(self, tasks_path: Path) -> list:
        """
        Load tasks from YAML file.

        Args:
            tasks_path: Path to tasks YAML file

        Returns:
            List of task configurations
        """
        with open(tasks_path, "r") as f:
            data = yaml.safe_load(f)

        task_source = data.get("task_source", "claude_web")
        tasks = data.get("tasks", [])

        # Normalize tasks format
        normalized = []
        for task in tasks:
            if isinstance(task, str):
                # Simple task name
                normalized.append(
                    {
                        "task_name": task,
                        "task_source": task_source,
                    }
                )
            elif isinstance(task, dict):
                # Full task config
                if "task_source" not in task:
                    task["task_source"] = task_source
                normalized.append(task)

        return normalized

    def run_task(
        self,
        task: dict,
        task_index: int,
        dry_run: bool = False,
        keep_temp_configs: bool = False,
        timeout: int = None,
    ) -> bool:
        """
        Run a single task.

        Args:
            task: Task configuration
            task_index: Task index (for logging)
            dry_run: If True, just print what would be executed
            keep_temp_configs: If True, don't delete temp config files
            timeout: Task timeout in seconds

        Returns:
            True if task succeeded
        """
        global _current_process

        task_name = task.get("task_name", f"task_{task_index}")
        logger.info(f"\n{'='*60}")
        logger.info(f"TASK {task_index}: {task_name}")
        logger.info(f"{'='*60}")

        # Merge with template
        config = self._merge_config(task)

        # Create temp config file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", prefix=f"claude_web_{task_name}_", delete=False
        ) as f:
            yaml.dump(config, f, default_flow_style=False)
            temp_config_path = f.name

        try:
            # Build command
            cmd = [
                *self.python_cmd,
                str(self.engine_script),
                "--config",
                temp_config_path,
                "--no-hold",
            ]

            if dry_run:
                logger.info(f"[DRY RUN] Would execute: {' '.join(cmd)}")
                logger.info(f"Config:\n{yaml.dump(config, default_flow_style=False)}")
                return True

            logger.info(f"Executing: {' '.join(cmd)}")

            # Run task
            _current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            # Stream output
            try:
                for line in iter(_current_process.stdout.readline, ""):
                    print(line, end="", flush=True)
            except Exception:
                pass

            # Wait for completion
            try:
                return_code = _current_process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.error(f"Task {task_name} timed out after {timeout}s")
                _current_process.terminate()
                try:
                    _current_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _current_process.kill()
                return False

            _current_process = None

            if return_code == 0:
                logger.info(f"Task {task_name} completed successfully")
                return True
            else:
                logger.error(
                    f"Task {task_name} failed with return code {return_code} "
                    f"(engine handles retries internally)"
                )
                return False

        finally:
            # Clean up temp config
            if not keep_temp_configs:
                try:
                    os.unlink(temp_config_path)
                except Exception:
                    pass

    def _find_run_directory(self) -> Path | None:
        """
        Find the most recent run directory matching today's date and folder prefix.

        Returns the Path if found, None otherwise.
        """
        output_config = self.template.get(
            "claude_web", self.template.get("chatgpt_web", {})
        ).get("output", {})
        base_dir = Path(output_config.get("base_dir", "."))

        provider_map = {"claude": "claudeGUI", "chatgpt": "chatgptGUI"}
        folder_prefix = output_config.get(
            "folder_prefix",
            provider_map.get(getattr(self, "provider", "claude"), "claudeGUI"),
        )

        date_str = datetime.now().strftime("%Y%m%d")
        run_dir = base_dir / f"{date_str}_{folder_prefix}"

        if run_dir.exists():
            return run_dir
        return None

    def write_batch_summary(self, results: dict) -> Path | None:
        """
        Write a human-readable batch summary text file.

        Reads completion JSONs from the run directory to get detailed failure
        reasons, then writes a summary to the run directory.

        Args:
            results: Results dict from run_all_tasks()

        Returns:
            Path to the summary file, or None if it couldn't be written.
        """
        run_dir = self._find_run_directory()
        if not run_dir:
            logger.warning("Could not find run directory for batch summary")
            return None

        json_logs_dir = run_dir / "json_logs"

        # Parse completion JSONs to get detailed per-task status
        task_details = {}  # task_name -> {status, reason, duration, attempt}
        if json_logs_dir.exists():
            for json_file in sorted(json_logs_dir.glob("completion_*.json")):
                try:
                    with open(json_file) as f:
                        data = json.load(f)
                    for task_entry in data.get("tasks", []):
                        name = task_entry.get("task_name", "unknown")
                        deprecated = task_entry.get("deprecated", False)
                        if deprecated:
                            continue  # Skip deprecated attempts
                        task_details[name] = {
                            "status": task_entry.get("task_status", "unknown"),
                            "reason": task_entry.get("agent_failed_reason"),
                            "duration": task_entry.get("duration_seconds"),
                            "attempt": task_entry.get("attempt_number", 1),
                        }
                except Exception as e:
                    logger.debug(f"Failed to parse {json_file.name}: {e}")

        # Build summary text
        lines = []
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"Batch Run Summary — {timestamp}")
        lines.append("=" * 70)
        lines.append("")
        lines.append(f"Total tasks:  {results['total']}")
        lines.append(f"Succeeded:    {results['succeeded']}")
        lines.append(f"Failed:       {results['failed']}")
        lines.append(f"Skipped:      {results['skipped']}")

        total_duration = sum(
            t.get("duration_seconds", 0) for t in results.get("tasks", [])
        )
        lines.append(f"Total time:   {total_duration / 60:.1f} min")
        lines.append("")
        lines.append("-" * 70)
        lines.append(f"{'#':<4} {'Task Name':<40} {'Status':<18} {'Time':>8}")
        lines.append("-" * 70)

        for i, task_result in enumerate(results.get("tasks", []), 1):
            name = task_result.get("task_name", "unknown")
            success = task_result.get("success", False)
            duration = task_result.get("duration_seconds", 0)
            duration_str = f"{duration / 60:.1f}m"

            # Get detailed status from completion JSON
            detail = task_details.get(name, {})
            if success:
                status_str = "SUCCESS"
            elif detail.get("status"):
                status_str = detail["status"].upper()
            else:
                status_str = "FAILED"

            lines.append(f"{i:<4} {name:<40} {status_str:<18} {duration_str:>8}")

        # Failed task details section
        failed_tasks = [
            t for t in results.get("tasks", []) if not t.get("success", False)
        ]
        if failed_tasks:
            lines.append("")
            lines.append("=" * 70)
            lines.append("FAILED TASK DETAILS")
            lines.append("=" * 70)

            for task_result in failed_tasks:
                name = task_result.get("task_name", "unknown")
                detail = task_details.get(name, {})
                status = detail.get("status", "unknown")
                reason = detail.get("reason", status)
                attempt = detail.get("attempt")

                lines.append("")
                lines.append(f"  Task:    {name}")
                lines.append(f"  Status:  {status}")
                lines.append(
                    f"  Reason:  {reason or 'Process exited with non-zero code'}"
                )
                if attempt:
                    lines.append(f"  Attempt: {attempt}")

        lines.append("")

        # Write to file
        summary_path = run_dir / "batch_summary.txt"
        try:
            with open(summary_path, "w") as f:
                f.write("\n".join(lines))
            logger.info(f"Batch summary written to: {summary_path}")
            return summary_path
        except Exception as e:
            logger.error(f"Failed to write batch summary: {e}")
            return None

    def run_all_tasks(
        self,
        tasks: list,
        dry_run: bool = False,
        start_index: int = 0,
        end_index: int = None,
        continue_on_failure: bool = True,
        default_timeout: int = None,
    ) -> dict:
        """
        Run all tasks sequentially.

        Args:
            tasks: List of task configurations
            dry_run: If True, just print what would be executed
            start_index: Start from this task index
            end_index: Stop at this task index (exclusive)
            continue_on_failure: Continue running tasks even if one fails
            default_timeout: Default timeout per task in seconds

        Returns:
            Dict with results summary
        """
        if end_index is None:
            end_index = len(tasks)

        tasks_to_run = tasks[start_index:end_index]
        logger.info(
            f"Running {len(tasks_to_run)} tasks (indices {start_index}-{end_index-1})"
        )

        results = {
            "total": len(tasks_to_run),
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "tasks": [],
        }

        for i, task in enumerate(tasks_to_run):
            task_index = start_index + i
            task_name = task.get("task_name", f"task_{task_index}")

            # Get timeout
            timeout = task.get("timeout", default_timeout)
            if timeout is None:
                timeout = task.get("claude_web", {}).get("max_sec_per_task")

            start_time = datetime.now()

            try:
                success = self.run_task(
                    task=task,
                    task_index=task_index,
                    dry_run=dry_run,
                    timeout=timeout,
                )
            except KeyboardInterrupt:
                logger.warning("Interrupted by user")
                results["skipped"] += len(tasks_to_run) - i - 1
                break
            except Exception as e:
                logger.error(f"Task {task_name} failed with exception: {e}")
                success = False

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            results["tasks"].append(
                {
                    "task_name": task_name,
                    "index": task_index,
                    "success": success,
                    "duration_seconds": duration,
                }
            )

            if success:
                results["succeeded"] += 1
            else:
                results["failed"] += 1
                if not continue_on_failure:
                    logger.error("Stopping due to failure (--stop-on-failure)")
                    results["skipped"] += len(tasks_to_run) - i - 1
                    break

        return results


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Web Agent Batch Runner - Run multiple tasks through Claude.ai or ChatGPT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tasks",
        required=True,
        help="Path to tasks YAML file",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Path to template YAML file (default: auto-selected by provider)",
    )
    parser.add_argument(
        "--provider",
        choices=["claude", "chatgpt"],
        default="claude",
        help="Web automation provider: 'claude' (default) or 'chatgpt'",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be executed without running",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Start from this task index",
    )
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="Stop at this task index (exclusive)",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop running if a task fails",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Default timeout per task in seconds",
    )
    args = parser.parse_args()

    # Signal handler
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        # Find template (provider-aware default)
        if not args.template:
            template_defaults = {
                "claude": "tasks_configs/template_claude_web.yaml",
                "chatgpt": "tasks_configs/template_chatgpt_web.yaml",
            }
            default_template_rel = template_defaults.get(
                args.provider, "tasks_configs/template_claude_web.yaml"
            )
            default_template = Path(__file__).parent / default_template_rel
            if default_template.exists():
                args.template = str(default_template)

        template_path = args.template

        if template_path:
            template_path = Path(template_path)
            if not template_path.exists():
                logger.error(f"Template not found: {template_path}")
                sys.exit(1)

        # Initialize runner
        runner = ClaudeWebBatchRunner(
            template_path=template_path,
        )
        runner.provider = args.provider

        # Load tasks from YAML file
        tasks_path = Path(args.tasks)
        if not tasks_path.exists():
            logger.error(f"Tasks file not found: {tasks_path}")
            sys.exit(1)
        tasks = runner.load_tasks(tasks_path)
        logger.info(f"Loaded {len(tasks)} tasks from {tasks_path}")

        # Run tasks
        results = runner.run_all_tasks(
            tasks=tasks,
            dry_run=args.dry_run,
            start_index=args.start,
            end_index=args.end,
            continue_on_failure=not args.stop_on_failure,
            default_timeout=args.timeout,
        )

        # Print summary
        logger.info("\n" + "=" * 60)
        logger.info("BATCH RUN COMPLETE")
        logger.info("=" * 60)
        logger.info(f"Total:     {results['total']}")
        logger.info(f"Succeeded: {results['succeeded']}")
        logger.info(f"Failed:    {results['failed']}")
        logger.info(f"Skipped:   {results['skipped']}")

        # Save results
        results_file = Path("claude_web_batch_results.json")
        with open(results_file, "w") as f:
            json.dump(results, f, indent=2)
        logger.info(f"Results saved to: {results_file}")

        # Write batch summary
        if not args.dry_run:
            runner.write_batch_summary(results)

        # Exit code
        if results["failed"] > 0:
            sys.exit(1)
        sys.exit(0)

    except Exception as e:
        logger.error(f"Batch runner failed: {e}")
        import traceback

        logger.debug(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
