#!/usr/bin/env python

import logging
import time


class Retryable(BaseException): pass


class retry(object):
    """Returns a callable that will retry the callee up to N times,
    if the callee raises an exception of type Retryable.
    To be clear: if N == 0, then the function will not retry.
    So, to get three tries, you must pass N == 2."""

    def __init__(self, N, timeout=0, retryable_exception=Retryable):
        self.N = N
        self.timeout = timeout
        self.retryable_exception = retryable_exception

    def __call__(self, kallable):

        def retryer(*a, **kw):
            logger = logging.getLogger("retry")
            while True:
                try:
                    return kallable(*a, **kw)
                except self.retryable_exception as e:
                    if self.N >= 1:
                        logger.error(
                            "Received retryable error %s running %s, trying %s more times",
                            e,
                            kallable,
                            self.N
                        )
                        time.sleep(self.timeout)
                    else:
                        raise
                self.N -= 1

        return retryer
