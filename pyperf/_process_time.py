"""
Similar to UNIX time command: measure the execution time of a command.

Minimum Python script spawning a program, wait until it completes, and then
write the elapsed time into stdout. Time is measured by the time.perf_counter()
timer.

Python subprocess.Popen() is implemented with fork()+exec(). Minimize the
Python imports to reduce the memory footprint, to reduce the cost of
fork()+exec().

Measure wall-time, not CPU time.

If resource.getrusage() is available: compute the maximum RSS memory in bytes
per process and writes it into stdout as a second line.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time

# Try importing resource module for memory usage tracking
try:
    import resource
except ImportError:
    resource = None

# Function to get maximum RSS (Resident Set Size) memory usage
def get_max_rss(*, children):
    if resource is not None:
        if children:
            resource_type = resource.RUSAGE_CHILDREN
        else:
            resource_type = resource.RUSAGE_SELF
        usage = resource.getrusage(resource_type)
        # macOS returns bytes, Linux returns kilobytes
        if sys.platform == 'darwin':
            return usage.ru_maxrss
        return usage.ru_maxrss * 1024
    else:
        return 0

# Merge two cProfile profiling statistics files
def merge_profile_stats_files(src, dst):
    """
    Merging one existing pstats file into another.
    """
    import pstats
    if os.path.isfile(dst):
        src_stats = pstats.Stats(src)
        dst_stats = pstats.Stats(dst)
        dst_stats.add(src_stats)
        dst_stats.dump_stats(dst)
        os.unlink(src)  # Delete source after merging
    else:
        os.rename(src, dst)  # If no destination exists, just move source

# Benchmark a process by running it multiple times and collecting timing and memory data
def bench_process(loops, args, kw, profile_filename=None):
    max_rss = 0
    range_it = range(loops)
    start_time = time.perf_counter()

    # If profiling is requested, create a temporary profile output file
    if profile_filename:
        temp_profile_filename = tempfile.mktemp()
        args = [args[0], "-m", "cProfile", "-o", temp_profile_filename] + args[1:]

    for _ in range_it:
        start_rss = get_max_rss(children=True)

        # Start the external process
        proc = subprocess.Popen(args, **kw)
        with proc:
            proc.wait()  # Wait for the process to complete

        exitcode = proc.returncode
        if exitcode != 0:
            print("Command failed with exit code %s" % exitcode,
                  file=sys.stderr)
            if profile_filename:
                os.unlink(temp_profile_filename)
            sys.exit(exitcode)

        # Update maximum observed memory usage
        rss = get_max_rss(children=True) - start_rss
        max_rss = max(max_rss, rss)

        # Merge profiling results if profiling is enabled
        if profile_filename:
            merge_profile_stats_files(
                temp_profile_filename, profile_filename
            )

    dt = time.perf_counter() - start_time
    return (dt, max_rss)

# Parse optional hook plugins from the command line arguments
def load_hooks(metadata):
    hook_names = []
    while "--hook" in sys.argv:
        hook_idx = sys.argv.index("--hook")
        hook_name = sys.argv[hook_idx + 1]
        hook_names.append(hook_name)
        del sys.argv[hook_idx]
        del sys.argv[hook_idx]

    if len(hook_names):
        # Import hooks module only if hooks are requested
        import pyperf._hooks

        hook_managers = pyperf._hooks.instantiate_selected_hooks(hook_names)
        metadata["hooks"] = ", ".join(hook_managers.keys())
    else:
        hook_managers = {}

    return hook_managers

# Write benchmark results to stdout in a structured format
def write_data(dt, max_rss, metadata, out=sys.stdout):
    # Three lines output:
    # 1. Runtime (seconds)
    # 2. Maximum RSS memory (or -1 if unavailable)
    # 3. Metadata as a JSON dictionary
    print(dt, file=out)
    print(max_rss or -1, file=out)
    json.dump(metadata, fp=out)
    print(file=out)

# Main function for script execution
def main():
    # Prevent users from wrongly running this internal module via -m option
    if 'pyperf' in sys.modules:
        print("ERROR: don't run %s -m pyperf._process, run the .py script"
              % os.path.basename(sys.executable))
        sys.exit(1)

    # Validate minimal argument count
    if len(sys.argv) < 3:
        print("Usage: %s %s loops program [arg1 arg2 ...] [--profile profile]"
              % (os.path.basename(sys.executable), __file__))
        sys.exit(1)

    # Check if profiling is requested via command line
    if "--profile" in sys.argv:
        profile_idx = sys.argv.index("--profile")
        profile_filename = sys.argv[profile_idx + 1]
        del sys.argv[profile_idx]
        del sys.argv[profile_idx]
    else:
        profile_filename = None

    metadata = {}
    hook_managers = load_hooks(metadata)

    # Extract the number of loops and the target command arguments
    loops = int(sys.argv[1])
    args = sys.argv[2:]

    kw = {}
    # Redirect stdin, stdout, stderr appropriately
    if hasattr(subprocess, 'DEVNULL'):
        devnull = None
        kw['stdin'] = subprocess.DEVNULL
        kw['stdout'] = subprocess.DEVNULL
    else:
        devnull = open(os.devnull, 'w+', 0)
        kw['stdin'] = devnull
        kw['stdout'] = devnull
    kw['stderr'] = subprocess.STDOUT

    # Handle multiple hooks cleanly using ExitStack
    with contextlib.ExitStack() as stack:
        for hook in hook_managers.values():
            stack.enter_context(hook)
        dt, max_rss = bench_process(loops, args, kw, profile_filename)

    if devnull is not None:
        devnull.close()

    # Call hook teardown methods
    for hook in hook_managers.values():
        hook.teardown(metadata)

    write_data(dt, max_rss, metadata)

# Entry point check
if __name__ == "__main__":
    main()
