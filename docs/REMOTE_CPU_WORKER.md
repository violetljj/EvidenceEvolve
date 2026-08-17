# Ephemeral CPU worker

EvidenceEvolve can use an SSH-accessible Linux machine as an execution-only CPU
worker while the controller, search policy, archive, and scientific authority stay
local. The worker never writes the local archive and its receipt explicitly has
`authority=EXECUTION_ONLY`.

## Authority boundary

The local controller creates a request bound to:

- one reachable Git commit;
- the SHA-256 of every declared critical input;
- an explicit entrypoint and argument vector (no shell command string);
- a CPU-worker ceiling, one numeric thread per worker, and a wall timeout;
- declared output paths.

The worker checks out only that commit in a job-specific directory, verifies every
bound input, executes the command without a shell, terminates the process tree on
timeout, and returns a create-once bundle containing logs, environment limits,
pre/post `pip freeze`, output artifacts, and hashes. A job that mutates the Python
environment fails closed. Local verification checks the request, commit, receipt,
logs, and artifacts before the bundle is usable.

This transport receipt is not an `EvaluationReceipt`, GateEngine verdict, promotion,
confirmation, or scientific result. A frozen adapter must still translate verified
execution output into the existing evaluation path. Do not send final-blind assets
to this general worker.

## One-time bootstrap

Bootstrap creates a Git bundle from the current local `HEAD`, transfers that exact
history over SSH, then creates a control checkout and virtual environment under the
data disk. The worker therefore does not require direct GitHub access:

```powershell
evolve-remote bootstrap `
  --host root@connect.example.com `
  --port 12345 `
  --remote-root /root/autodl-tmp/evidence-evolve-worker
```

The default remote Python is `/root/miniconda3/bin/python`. Bootstrap refuses a
dirty existing control checkout instead of overwriting it. Dispatch likewise sends
a Git bundle for the request commit; the request commit must still be local `HEAD`.
Exact installed versions are captured before and after every job.

## Create and dispatch an eight-worker canary

```powershell
evolve-remote create-job `
  --job-id CPU-CANARY-001 `
  --entrypoint pytest `
  --input pyproject.toml `
  --input tests/test_throughput.py `
  --workers 8 `
  --timeout-seconds 600 `
  --output runs/remote_cpu_canary/request.json `
  -- -q -n 8 tests/test_throughput.py

evolve-remote dispatch `
  runs/remote_cpu_canary/request.json `
  --host root@connect.example.com `
  --port 12345 `
  --result-dir runs/remote_cpu_canary/result

evolve-remote verify-result `
  runs/remote_cpu_canary/request.json `
  runs/remote_cpu_canary/result
```

For an `evolve` command, use `--entrypoint evolve` and put the ordinary CLI
arguments after `--`. Bind the frozen contract, evaluator, candidate, and data
manifest with repeated `--input`. Add each required result directory with repeated
`--output-path`; a missing declared output makes the job fail closed.

## Operating rules

- Start with eight workers. Increase only after measuring CPU affinity, memory,
  disk, wall time, and throughput on the real evaluator.
- Worker flags such as `-n`, `--workers`, and `--max-workers` may not exceed the
  request's CPU ceiling.
- Each candidate retains its own job checkout and output root. Do not share mutable
  worktrees or create-once roots between live writers.
- There are no automatic retries or replacement samples. A new attempt needs a new
  job identity and must remain visible in campaign accounting.
- The worker is not a hostile-code sandbox. Keep credentials and blind assets off
  the disposable instance; re-bootstrap it if candidate code changes the environment.
- Stop billing through the provider control plane after verified result retrieval;
  exiting a process or closing SSH is not proof that billing stopped.
