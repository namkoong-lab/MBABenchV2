#!/usr/bin/env python3
"""
Local Batch Runner for Excel CLI Agent

Runs tasks from local folders without DB or S3 dependencies.
Creates fresh workspaces, executes the agent, saves results locally,
and logs attempts to a JSONL file.
"""

import json
import os
import shutil
import time
import yaml
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

from .batch_runner import BatchRunner, WorkspaceConfig, WorkspaceResult, BatchResult
from .prompt_versions import PROMPTS_DIR, PROMPT_VERSIONS, DEFAULT_PROMPT_VERSION, parse_prompt_version


class LocalBatchRunner(BatchRunner):
    """Local batch runner — no DB, no S3. Reads from local folders, saves results locally."""

    def __init__(self, config_path: str, server_path: str, api_key: str,
                 custom_reasoning: bool = False, enable_langfuse: bool = False):
        super().__init__(config_path, server_path, api_key, custom_reasoning, enable_langfuse)
        self._system_prompt_path: Optional[Path] = None
        self._task_template_path: Optional[Path] = None

    def load_config(self) -> Dict[str, Any]:
        """Load and validate YAML configuration for local mode."""
        print(f"📋 Loading local batch configuration from {self.config_path}")

        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Validate required fields
        if 'batch_name' not in config:
            raise ValueError("Missing required field: batch_name")
        if 'model' not in config:
            raise ValueError("Missing required field: model")
        if 'workspaces' not in config:
            raise ValueError("Missing required field: workspaces (list of {path: ...})")

        # Set defaults
        config.setdefault('verbose', False)
        config.setdefault('max_iterations', 30)
        config.setdefault('batch_size', 1)
        config.setdefault('snapshot_iterations', False)
        config.setdefault('workspace_base_dir', './workspaces')
        config.setdefault('results_dir', './results')
        config.setdefault('cleanup_workspace', False)
        config.setdefault('task_type', 'fmwc')  # fmwc or wsp, for template selection

        # Resolve prompt version
        prompt_ver = config.get('prompt_version', DEFAULT_PROMPT_VERSION)
        if prompt_ver not in PROMPT_VERSIONS:
            raise ValueError(f"Unknown prompt_version '{prompt_ver}'. Available: {list(PROMPT_VERSIONS.keys())}")
        ver_files = PROMPT_VERSIONS[prompt_ver]

        task_type = config['task_type']
        template_key = 'wsp' if task_type == 'wsp' else 'fmwc'

        self._system_prompt_path = PROMPTS_DIR / ver_files["system"]
        self._task_template_path = PROMPTS_DIR / ver_files[template_key]
        config['system_prompt_path'] = str(self._system_prompt_path)

        # Load task template
        if 'task_template' not in config:
            if self._task_template_path.exists():
                config['task_template'] = self._task_template_path.read_text(encoding='utf-8')
            else:
                raise FileNotFoundError(f"Task template not found: {self._task_template_path}")

        self.config = config

        print(f"✅ Configuration loaded: {config['batch_name']}")
        print(f"   Model: {config['model']}")
        print(f"   Prompt version: {prompt_ver}")
        print(f"   Max iterations: {config['max_iterations']}")
        print(f"   Workspaces: {len(config['workspaces'])}")
        print(f"   Results dir: {config['results_dir']}")

        return config

    def setup_workspace(self, source_path: str) -> str:
        """Create a fresh workspace and copy source files into it."""
        source = Path(source_path).expanduser().resolve()
        if not source.exists():
            raise ValueError(f"Source path does not exist: {source_path}")

        task_name = source.name
        base_dir = Path(self.config['workspace_base_dir']).expanduser()
        run_id = f"{int(time.time())}_{os.getpid()}"
        workspace = base_dir / f"{task_name}_{run_id}"
        workspace.mkdir(parents=True, exist_ok=True)

        print(f"  📁 Workspace: {workspace}")

        # Copy all files from source
        for f in source.iterdir():
            if f.is_file():
                shutil.copy2(f, workspace / f.name)
                print(f"  📄 Copied: {f.name}")

        return str(workspace)

    def save_results(self, task_name: str, workspace_path: str, result: WorkspaceResult):
        """Copy result files to results_dir and append to attempts.jsonl."""
        results_dir = Path(self.config['results_dir']).expanduser()
        task_results_dir = results_dir / task_name
        task_results_dir.mkdir(parents=True, exist_ok=True)

        workspace = Path(workspace_path)

        # Copy result files
        files_copied = []
        # solution.xlsx
        solution = workspace / "solution.xlsx"
        if solution.exists():
            shutil.copy2(solution, task_results_dir / "solution.xlsx")
            files_copied.append("solution.xlsx")

        # agent_logs contents
        agent_logs = workspace / "agent_logs"
        if agent_logs.exists():
            csv_path = agent_logs / "openai_requests.csv"
            if csv_path.exists():
                shutil.copy2(csv_path, task_results_dir / "openai_requests.csv")
                files_copied.append("openai_requests.csv")

            for task_dir in sorted(agent_logs.iterdir()):
                if task_dir.is_dir() and task_dir.name.startswith("task_"):
                    for fname in ["task.json", "transcript.md"]:
                        fpath = task_dir / fname
                        if fpath.exists():
                            shutil.copy2(fpath, task_results_dir / fname)
                            files_copied.append(fname)

        for f in files_copied:
            print(f"  📤 Saved: {task_results_dir / f}")

        # Compute prompt version
        prompt_version = None
        if self._system_prompt_path and self._task_template_path:
            prompt_version = parse_prompt_version(self._system_prompt_path, self._task_template_path)

        # Append to attempts.jsonl
        attempt = {
            "task_name": task_name,
            "model": self.config['model'],
            "start_time": datetime.fromtimestamp(result.start_time).isoformat() if result.start_time else None,
            "end_time": datetime.fromtimestamp(result.end_time).isoformat() if result.end_time else None,
            "time_taken_min": result.duration_seconds / 60.0,
            "cost": round(result.cost_usd, 6),
            "status": result.status,
            "error": result.error_message,
            "prompt_version": prompt_version,
            "iterations": result.iterations,
            "result_dir": str(task_results_dir),
        }

        attempts_file = results_dir / "attempts.jsonl"
        with open(attempts_file, "a") as f:
            f.write(json.dumps(attempt) + "\n")

        print(f"  📝 Logged attempt to {attempts_file}")

    def cleanup_workspace(self, workspace_path: str):
        """Delete workspace after results are saved."""
        if not self.config.get('cleanup_workspace', False):
            print(f"  📂 Workspace preserved: {workspace_path}")
            return
        try:
            shutil.rmtree(workspace_path)
            print(f"  🧹 Cleaned up workspace: {workspace_path}")
        except Exception as e:
            print(f"  ⚠️  Cleanup failed: {e}")

    def run_batch(self) -> BatchResult:
        """Execute local batch: copy files -> execute -> save results."""
        batch_start_time = time.time()

        config = self.load_config()

        # Setup batch logging directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.batch_logs_dir = Path("batch_logs") / f"batch_{timestamp}"
        self.batch_logs_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n📂 Batch logs directory: {self.batch_logs_dir}")

        # Ensure results dir exists
        results_dir = Path(config['results_dir']).expanduser()
        results_dir.mkdir(parents=True, exist_ok=True)

        workspace_results = []

        for idx, ws_entry in enumerate(config['workspaces']):
            source_path = os.path.expanduser(ws_entry['path'])
            task_name = Path(source_path).name

            print(f"\n{'='*80}")
            print(f"📦 Task {idx + 1}/{len(config['workspaces'])}: {task_name}")
            print(f"{'='*80}")

            workspace_path = None
            try:
                # Setup fresh workspace
                workspace_path = self.setup_workspace(source_path)

                # Detect files and process
                ws_config = self.detect_workspace_files(workspace_path)
                result = self.process_workspace(ws_config)
                workspace_results.append(result)

                # Save results locally
                print(f"\n  📤 Saving results...")
                self.save_results(task_name, workspace_path, result)

                # Cleanup
                self.cleanup_workspace(workspace_path)

            except Exception as e:
                print(f"  💥 Error processing '{task_name}': {e}")
                workspace_results.append(WorkspaceResult(
                    workspace_path=workspace_path or source_path,
                    status="error",
                    pdf_files=[],
                    excel_files=[],
                    task_id=None,
                    iterations=0,
                    total_tokens=0,
                    cost_usd=0.0,
                    error_message=str(e),
                    duration_seconds=0,
                    final_result=None,
                ))

        # Aggregated metrics
        total_duration = time.time() - batch_start_time
        successful = sum(1 for r in workspace_results if r.status == "success")
        failed = len(workspace_results) - successful
        total_tokens = sum(r.total_tokens for r in workspace_results)
        total_iterations = sum(r.iterations for r in workspace_results)
        total_cost = sum(r.cost_usd for r in workspace_results)

        batch_result = BatchResult(
            batch_name=config['batch_name'],
            total_workspaces=len(workspace_results),
            successful=successful,
            failed=failed,
            workspace_results=workspace_results,
            total_duration_seconds=total_duration,
            aggregated_tokens=total_tokens,
            aggregated_iterations=total_iterations,
            aggregated_cost_usd=total_cost,
        )

        self.generate_reports(batch_result)

        return batch_result


def run_local_batch_from_config(config_path: str, server_path: str, api_key: str,
                                 custom_reasoning: bool = False, enable_langfuse: bool = False) -> BatchResult:
    """Entry point for local batch execution."""
    runner = LocalBatchRunner(config_path, server_path, api_key, custom_reasoning, enable_langfuse)
    return runner.run_batch()
