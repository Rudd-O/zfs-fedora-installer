"""Retry functionality."""

from collections.abc import Callable
import logging
import time
from typing import Any, cast


class Retryable(BaseException):
    """Type of exception that can be retried."""

    pass


class retry:
    """Retry a particular callable.

    Returns a callable that will retry the callee up to N times,
    if the callee raises an exception of type Retryable.
    To be clear: if N == 0, then the function will not retry.
    So, to get three tries, you must pass N == 2.
    """

    def __init__(
        self,
        N: int,
        timeout: int | float = 0,
        retryable_exception: type[BaseException] = Retryable,
    ) -> None:
        """Initialize the retrier.

        Args:
        N: number of retries (0 = no retry)
        timeout: time to sleep between retries
        retryable_exception: type of exception to retry
        """
        self.N = N
        self.timeout = timeout
        self.retryable_exception = retryable_exception

    def __call__[F: Callable[..., Any]](self, kallable: F) -> F:
        """Return a function that will retry the callable."""

        def retryer(*a: Any, **kw: Any) -> Any:
            logger = logging.getLogger("retry")
            while True:
                try:
                    return kallable(*a, **kw)
                except self.retryable_exception as e:
                    if self.N >= 1:
                        logger.error(
                            "Received retryable error %s running %s, "
                            "trying %s more times",
                            e,
                            kallable,
                            self.N,
                        )
                        time.sleep(self.timeout)
                    else:
                        raise
                self.N -= 1

        return cast(F, retryer)
