"""引擎适配器与执行上下文定义。"""

from spectra_workbench.providers.base import (
    CommitTransactionError,
    ExecutionContext,
    InputOutputConflictError,
    OutputConflictError,
    ProviderAdapter,
    TaskCancelledError,
    resolve_dataset_path,
)

__all__ = [
    "CommitTransactionError",
    "ExecutionContext",
    "InputOutputConflictError",
    "OutputConflictError",
    "ProviderAdapter",
    "TaskCancelledError",
    "resolve_dataset_path",
]
