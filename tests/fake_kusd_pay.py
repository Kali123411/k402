#!/usr/bin/env python3
"""A stand-in for the kusd_pay binary: answers from FAKE_KUSD_RESULTS (JSON: command -> result) and
records every invocation's argv in FAKE_KUSD_LOG. Never touches a chain."""
import json
import os
import sys

results = json.loads(os.environ.get("FAKE_KUSD_RESULTS", "{}"))
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
log = os.environ.get("FAKE_KUSD_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
if cmd not in results:
    print(json.dumps({"error": f"fake has no result for {cmd}"}))
    sys.exit(1)
out = results[cmd]
print(json.dumps(out), flush=True)
# FAKE_KUSD_SLEEP: linger after printing (the real binary publishes to the relay after it prints its result)
import time
time.sleep(float(os.environ.get("FAKE_KUSD_SLEEP", "0")))
sys.exit(int(results.get(cmd + "_exit", 0)))
