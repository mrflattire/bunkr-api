import asyncio
import sys


def run_async(coro):
    """Run a coroutine to completion from synchronous CLI code.

    On Windows this uses the Selector event loop, because curl_cffi does not
    support the default Proactor loop. Every CLI entry point goes through
    here so that rule lives in exactly one place.
    """
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)
