"""Arms the benchmark's timing wrappers in every process that inherits this
directory on PYTHONPATH — the server and each worker it starts, whichever
start method multiprocessing uses. A no-op without KALINKA_BENCH_TIMING=1.
"""

import os

if os.environ.get("KALINKA_BENCH_TIMING") == "1":
    try:
        import bench_timing

        bench_timing.install()
    except Exception:  # never keep the server from starting
        import traceback

        traceback.print_exc()
