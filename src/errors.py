"""Typed failures shared across harness boundaries."""


class HarnessError(Exception):
    code = "HARNESS_ERROR"


class PersistenceError(HarnessError):
    code = "PERSISTENCE_ERROR"


class ConflictError(HarnessError):
    code = "CONFLICT"


class NotFoundError(HarnessError):
    code = "NOT_FOUND"


class ToolError(HarnessError):
    code = "TOOL_ERROR"


class UnknownToolError(ToolError):
    code = "UNKNOWN_TOOL"


class UnknownServiceError(ToolError):
    code = "UNKNOWN_SERVICE"


class ModelProviderError(HarnessError):
    code = "MODEL_PROVIDER_ERROR"


class ExecutionLimit(HarnessError):
    code = "EXECUTION_LIMIT"


class WriteOutcomeUnknown(HarnessError):
    code = "WRITE_OUTCOME_UNKNOWN"
