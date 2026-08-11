"""Command-line entry points for safe Camare deployment operations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .checks import CheckResult, run_doctor
from .config import ConfigError, load_config
from .model import DeploymentConfig
from .render import RenderError, render_runtime
from .runtime import (
    ApplyError,
    ApplyResult,
    apply_staging,
    validate_staging,
)
from .verify import VerificationResult, verify_runtime


SOURCE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path("camare.ini")


class CLIError(RuntimeError):
    """A concise command-line failure that is safe to show to a user."""


class RuntimeCommandRunner:
    """Run Compose commands without forwarding potentially sensitive output."""

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> None:
        try:
            completed = subprocess.run(
                arguments,
                cwd=cwd,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except FileNotFoundError as error:
            raise CLIError("Docker Compose is unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise CLIError("Docker Compose command timed out") from error
        if completed.returncode != 0:
            raise CLIError("Docker Compose rejected the rendered deployment")


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="path to camare.ini (default: ./camare.ini)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON without secrets",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="camarectl",
        description="Safely configure and operate one Camare instance.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser(
        "init", help="create an editable configuration from the example"
    )
    initialize.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="new config path"
    )

    for command, help_text in (
        ("validate", "validate configuration without reading secret values"),
        ("doctor", "run read-only host and camera preflight checks"),
        ("plan", "render and validate an isolated temporary deployment"),
        ("verify", "verify containers, streams, recordings and entrypoints"),
        ("status", "show a concise verified runtime status"),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        _add_common_options(subparser)

    apply = subparsers.add_parser(
        "apply", help="transactionally install or update one instance"
    )
    _add_common_options(apply)
    apply.add_argument(
        "--yes",
        action="store_true",
        help="approve the scoped instance change without an interactive prompt",
    )
    apply.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="bounded Compose build/start timeout in seconds (default: 900)",
    )
    return parser


def _json_dump(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _configuration_summary(config: DeploymentConfig) -> dict[str, object]:
    enabled_groups = tuple(group for group in config.groups if group.enabled)
    return {
        "hardware": config.hardware.mode,
        "instance_id": config.instance_id,
        "provider": config.provider,
        "groups": len(enabled_groups),
        "streams": sum(len(group.streams) for group in enabled_groups),
        "retention_days": config.retention,
    }


def _emit_configuration(config: DeploymentConfig, as_json: bool) -> None:
    summary = _configuration_summary(config)
    if as_json:
        _json_dump(summary)
        return
    print(
        "Configuration valid: "
        f"instance={summary['instance_id']} provider={summary['provider']} "
        f"groups={summary['groups']} streams={summary['streams']} "
        f"hardware={summary['hardware']}"
    )


def _result_dict(result: CheckResult | VerificationResult) -> dict[str, str]:
    return {
        "level": result.level,
        "code": result.code,
        "message": result.message,
        "detail": result.detail,
    }


def _emit_results(
    results: Sequence[CheckResult | VerificationResult], as_json: bool
) -> None:
    if as_json:
        _json_dump([_result_dict(result) for result in results])
        return
    for result in results:
        suffix = f" ({result.detail})" if result.detail else ""
        print(f"[{result.level}] {result.code}: {result.message}{suffix}")


def _has_failure(results: Sequence[CheckResult | VerificationResult]) -> bool:
    return any(result.level == "FAIL" for result in results)


def _safe_existing_runtime(config: DeploymentConfig, target: Path) -> bool:
    """Recognize only this instance before treating occupied ports as expected."""

    manifest = target / "manifest.json"
    try:
        if target.is_symlink() or manifest.is_symlink() or manifest.stat().st_size > 1 << 20:
            return False
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(document, dict)
        and document.get("format_version") == 1
        and document.get("instance_id") == config.instance_id
    )


def _apply_preflight(
    config: DeploymentConfig,
    results: Sequence[CheckResult],
) -> tuple[CheckResult, ...]:
    target = Path(str(config.runtime_dir))
    existing = _safe_existing_runtime(config, target)
    normalized: list[CheckResult] = []
    for result in results:
        if existing and result.level == "FAIL" and result.code.startswith("port."):
            normalized.append(
                CheckResult(
                    "WARN",
                    result.code,
                    "Port is already occupied by the existing instance; "
                    "post-start verification will confirm ownership",
                    result.detail,
                )
            )
        else:
            normalized.append(result)
    return tuple(normalized)


def _initialize(config_path: Path) -> int:
    destination = config_path.expanduser().absolute()
    example = SOURCE_ROOT / "camare.example.ini"
    if destination.exists() or destination.is_symlink():
        raise CLIError(f"configuration already exists: {destination}")
    if not example.is_file():
        raise CLIError("camare.example.ini is missing from this checkout")
    destination.parent.mkdir(parents=True, exist_ok=True)
    secrets = destination.parent / "secrets"
    if secrets.is_symlink() or (secrets.exists() and not secrets.is_dir()):
        raise CLIError("the configuration secrets path is unsafe")
    secrets.mkdir(mode=0o700, exist_ok=True)
    secrets.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.camare-init-",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(example.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise CLIError(f"configuration already exists: {destination}") from error
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Created {destination}")
    print("Next: edit it, create the referenced 0600 secret files, then run:")
    print(f"  python3 -m camarectl validate --config {destination}")
    print(f"  python3 -m camarectl doctor --config {destination}")
    print(f"  sudo python3 -m camarectl apply --config {destination}")
    return 0


def _plan(config: DeploymentConfig, as_json: bool) -> int:
    runner = RuntimeCommandRunner()
    with tempfile.TemporaryDirectory(prefix="camare-plan-") as temporary:
        layout = render_runtime(
            config,
            source_root=SOURCE_ROOT,
            staging_parent=Path(temporary),
        )
        validation = validate_staging(layout, runner)
        enabled_groups = tuple(group for group in config.groups if group.enabled)
        payload = {
            "hardware": config.hardware.mode,
            "instance_id": config.instance_id,
            "project_name": validation.project_name,
            "provider": config.provider,
            "services": 4 if config.provider == "cloudflare" else 3,
            "groups": len(enabled_groups),
            "streams": sum(len(group.streams) for group in enabled_groups),
            "compose_files": len(validation.compose_files),
        }
    if as_json:
        _json_dump(payload)
    else:
        print(
            "Plan valid: "
            f"project={payload['project_name']} services={payload['services']} "
            f"groups={payload['groups']} streams={payload['streams']}"
        )
        print("Temporary controls were removed; source and runtime were not changed.")
    return 0


def _confirmation_approved(assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise CLIError("apply requires --yes when standard input is non-interactive")
    print("This will install or update only the configured Camare instance.")
    return input("Type APPLY to continue: ").strip() == "APPLY"


def _apply(
    config: DeploymentConfig,
    *,
    as_json: bool,
    assume_yes: bool,
    timeout: float,
) -> int:
    if not 1 <= timeout <= 3600:
        raise CLIError("--timeout must be between 1 and 3600 seconds")
    preflight = _apply_preflight(config, run_doctor(config))
    if _has_failure(preflight):
        if as_json:
            _json_dump(
                {
                    "error": "preflight failed",
                    "checks": [_result_dict(result) for result in preflight],
                }
            )
        else:
            print("Preflight failed; no deployment files or containers were changed.")
            _emit_results(preflight, False)
        return 1
    warnings = tuple(result for result in preflight if result.level == "WARN")
    if warnings and not as_json:
        print("Preflight warnings:")
        _emit_results(warnings, False)
    if not _confirmation_approved(assume_yes):
        raise CLIError("apply was not confirmed")

    target = Path(str(config.runtime_dir))
    staging_parent = target.parent / ".camare-staging"
    if staging_parent.is_symlink():
        raise CLIError("the staging parent is an unsafe symlink")
    layout = render_runtime(
        config,
        source_root=SOURCE_ROOT,
        staging_parent=staging_parent,
    )
    runner = RuntimeCommandRunner()

    def verifier(runtime_root: Path) -> bool:
        return not _has_failure(verify_runtime(config, runtime_root))

    result: ApplyResult = apply_staging(
        layout,
        target,
        runner,
        verifier=verifier,
        command_timeout=timeout,
    )
    try:
        staging_parent.rmdir()
    except OSError:
        pass
    payload = {
        "changed": result.changed,
        "installed": result.installed,
        "instance_id": config.instance_id,
        "project_name": result.project_name,
        "verified": result.verified,
        "warnings": [warning.code for warning in warnings],
    }
    if as_json:
        _json_dump(payload)
    else:
        action = "installed" if result.installed else "updated"
        if not result.changed:
            action = "already current"
        print(
            f"Camare {action}: project={result.project_name}; "
            f"verified={'yes' if result.verified else 'not requested'}"
        )
    return 0


def _verify(config: DeploymentConfig, as_json: bool, *, concise: bool) -> int:
    results = verify_runtime(config, Path(str(config.runtime_dir)))
    failed = sum(result.level == "FAIL" for result in results)
    warned = sum(result.level == "WARN" for result in results)
    passed = sum(result.level == "PASS" for result in results)
    if concise:
        level = "FAIL" if failed else "WARN" if warned else "PASS"
        payload = {
            "level": level,
            "instance_id": config.instance_id,
            "passed": passed,
            "warnings": warned,
            "failed": failed,
        }
        if as_json:
            _json_dump(payload)
        else:
            print(
                f"[{level}] {config.instance_id}: "
                f"{passed} passed, {warned} warnings, {failed} failed"
            )
    else:
        _emit_results(results, as_json)
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    as_json = bool(getattr(arguments, "json", False))
    try:
        if arguments.command == "init":
            return _initialize(arguments.config)
        config = load_config(arguments.config)
        if arguments.command == "validate":
            _emit_configuration(config, as_json)
            return 0
        if arguments.command == "doctor":
            results = run_doctor(config)
            _emit_results(results, as_json)
            return 1 if _has_failure(results) else 0
        if arguments.command == "plan":
            return _plan(config, as_json)
        if arguments.command == "apply":
            return _apply(
                config,
                as_json=as_json,
                assume_yes=arguments.yes,
                timeout=arguments.timeout,
            )
        if arguments.command == "verify":
            return _verify(config, as_json, concise=False)
        if arguments.command == "status":
            return _verify(config, as_json, concise=True)
        raise CLIError(f"unknown command: {arguments.command}")
    except (ConfigError, RenderError, ApplyError, CLIError, OSError, ValueError) as error:
        if as_json:
            _json_dump({"error": str(error)})
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        return 130


__all__ = ["main"]
