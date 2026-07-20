#!/usr/bin/env python3
"""Base model class for evaluation scripts."""

import time
from abc import ABC, abstractmethod

import weave
from weave import Model

from .utils import TimeoutException


class BaseEvalModel(Model, ABC):
    """
    Abstract base class for evaluation models.

    All evaluation models should inherit from this class and implement the predict method.

    Common attributes:
        root_path: Project root path
        timeout: Timeout in seconds for processing a single repository
    """

    root_path: str
    timeout: int

    def _check_timeout(self, start_time: float, step_name: str = "") -> None:
        """
        Check whether the operation has timed out.

        Args:
            start_time: Start time.
            step_name: Current step name (for logging).

        Raises:
            TimeoutException: If the timeout is exceeded.
        """
        elapsed = time.time() - start_time
        if elapsed > self.timeout:
            msg = f"Timeout ({elapsed:.1f}s > {self.timeout}s)"
            if step_name:
                msg += f" at {step_name}"
            raise TimeoutException(msg)

    @weave.op
    @abstractmethod
    def predict(self, full_name: str) -> dict:
        """
        Process a single repository and return its status.

        Args:
            repo: Repository info, in the form {"repo": {"full_name": "...", ...}}

        Returns:
            Result dict, with at least:
            {
                "status": "success" | "error" | "timeout",
                "root_path": str,
                "full_name": str,
                ...  # model-specific fields
            }
        """
        pass
