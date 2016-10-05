#!/usr/bin/env python

import logging


class Retryable: pass


class retry(object):
    """Returns a callable that will retry the callee up to N times,
    if the callee raises an exception of type Retryable.
    To be clear: if N == 0, then the function will not retry.
    So, to get three tries, you must pass N == 2."""

    def __init__(self, N):
        self.N = N

    def __call__(self, kallable):

        def retryer(*a, **kw):
            logger = logging.getLogger("retry")
            while True:
                try:
                    return kallable(*a, **kw)
                except Retryable:
                    if self.N >= 1:
                        logger.error(
                            "Received retryable error running %s, trying %s more times",
                            kallable,
                            self.N
                        )
                    else:
                        raise
                self.N -= 1

        return retryer
