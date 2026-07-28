from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE_OR_CONFIGURATION = 2
    DEPENDENCY_UNAVAILABLE = 3
    VALIDATION_ERROR = 4
    APPROVAL_REQUIRED = 5
    BUDGET_BLOCKED = 6
    EXTERNAL_PROCESS_FAILURE = 7
    PROVIDER_TRANSIENT_FAILURE = 8
    PROVIDER_PERMANENT_FAILURE = 9
    QA_FAILED = 10
    STATE_CONFLICT = 11
    CANCELLED = 12
    INTERNAL_ERROR = 13


class VideoeditError(Exception):
    """Base error with a stable code and safe operator message."""

    def __init__(self, code: str, message: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class ProcessExecutionError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("process_start_failed", message, ExitCode.EXTERNAL_PROCESS_FAILURE)


class ProcessTimeoutError(VideoeditError):
    def __init__(self, executable: str, timeout_seconds: float, stderr: str = "") -> None:
        detail = f": {stderr[-500:]}" if stderr else ""
        message = f"{executable} exceeded its {timeout_seconds:g}s timeout{detail}"
        super().__init__("process_timeout", message, ExitCode.EXTERNAL_PROCESS_FAILURE)


class ProcessCancelledError(VideoeditError):
    def __init__(self, executable: str, stderr: str = "") -> None:
        detail = f": {stderr[-500:]}" if stderr else ""
        super().__init__(
            "process_cancelled", f"{executable} was cancelled{detail}", ExitCode.CANCELLED
        )


class WorkerContractError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("worker_contract_invalid", message, ExitCode.VALIDATION_ERROR)


class WorkerProcessError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("worker_process_failed", message, ExitCode.EXTERNAL_PROCESS_FAILURE)


class StateConflictError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("project_locked", message, ExitCode.STATE_CONFLICT)


class DiskSpaceError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("insufficient_disk_space", message, ExitCode.DEPENDENCY_UNAVAILABLE)


class SourceIntegrityError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("source_integrity_failed", message, ExitCode.VALIDATION_ERROR)


class MediaValidationError(VideoeditError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, ExitCode.VALIDATION_ERROR)


class DependencyUnavailableError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("dependency_unavailable", message, ExitCode.DEPENDENCY_UNAVAILABLE)


class TranscriptionOutputError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("transcription_output_invalid", message, ExitCode.VALIDATION_ERROR)


class SilenceDetectionError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("silence_detection_invalid", message, ExitCode.VALIDATION_ERROR)


class PlanningValidationError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("planning_invalid", message, ExitCode.VALIDATION_ERROR)


class ApprovalRequiredError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("approval_required", message, ExitCode.APPROVAL_REQUIRED)


class StaleApprovalError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("approval_stale", message, ExitCode.APPROVAL_REQUIRED)


class BudgetBlockedError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("budget_blocked", message, ExitCode.BUDGET_BLOCKED)


class ProviderTransientError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("provider_transient_failure", message, ExitCode.PROVIDER_TRANSIENT_FAILURE)


class ProviderPermanentError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("provider_permanent_failure", message, ExitCode.PROVIDER_PERMANENT_FAILURE)


class LoudnessMeasurementError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("loudness_measurement_invalid", message, ExitCode.VALIDATION_ERROR)


class RenderOutputError(VideoeditError):
    def __init__(self, message: str) -> None:
        super().__init__("render_output_invalid", message, ExitCode.VALIDATION_ERROR)


class ForegroundValidationError(MediaValidationError):
    def __init__(self, message: str) -> None:
        super().__init__("foreground_validation_failed", message)


class OccluderValidationError(MediaValidationError):
    def __init__(self, message: str) -> None:
        super().__init__("occluder_validation_failed", message)


class InpaintingValidationError(MediaValidationError):
    def __init__(self, message: str) -> None:
        super().__init__("inpainting_validation_failed", message)


class MaskValidationError(MediaValidationError):
    def __init__(self, message: str) -> None:
        super().__init__("mask_validation_failed", message)


class SegmentationValidationError(MediaValidationError):
    def __init__(self, message: str) -> None:
        super().__init__("segmentation_validation_failed", message)
