"""OpenCode with an inactivity watchdog.

OpenCode occasionally goes completely silent after finishing its work instead of
exiting: measured over a 20-trial run, 9 of 20 trials emitted their last event
and then held the pod for another 33-60 minutes until the agent timeout fired.
Every trial did all of its real work within 27.4 minutes of its first event, so
the dead air was pure waste -- 78% of the run's total pod-time.

Harbor's agent timeout is total wall clock, which cannot tell a slow trial from a
hung one. This subclass wraps the ``opencode run`` invocation in a shell watchdog
that watches the size of the streamed log and kills the process group once it has
not grown for ``idle_timeout_sec``. The trial then proceeds to verification with
whatever the agent already wrote, instead of burning the rest of the cap.

It also carries ``max_blocking_threads``, which raises the asyncio default thread
pool. That pool -- not the namespace quota -- is what actually caps concurrency on
Kubernetes: every in-pod command runs through ``asyncio.to_thread`` and holds a
worker for its full duration, and the default is ``min(32, cpu_count + 4)``.

Use it via ``import_path`` in a job config::

    agents:
      - import_path: bespoke_stratos_traj_analysis.opencode_watchdog:OpenCodeIdleWatchdog
        model_name: nrp/qwen3-small
        kwargs:
          idle_timeout_sec: 600
          max_blocking_threads: 256

The module must be importable by the ``harbor`` process, e.g.::

    PYTHONPATH=src uv run --project "$HARBOR_DIR" harbor run ...   # from setup_bespoke/
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, override

from harbor.agents.installed.opencode import OpenCode
from harbor.environments.base import BaseEnvironment
from harbor.utils.logger import logger

# Log file the parent class tees the OpenCode JSON stream into. Growth of this
# file is the liveness signal.
_STREAM_LOG = "/logs/agent/opencode.txt"

# Substring identifying the `opencode run` invocation among the several commands
# the parent class executes (installs, MCP setup, ...). Only that one is wrapped.
_RUN_MARKER = " run --format=json "

# Heredoc delimiter. The wrapped command embeds a shell-quoted instruction that
# contains newlines and single quotes, so it is passed through an unexpanded
# heredoc rather than being re-quoted.
_DELIM = "HB_OPENCODE_CMD_9f3a1c"

# Guard so the executor is swapped once per process, not once per trial.
_executor_raised_to: int | None = None


def _raise_blocking_thread_limit(max_workers: int) -> bool:
    """Enlarge the running loop's default thread pool.

    ``ACKEnvironment._exec`` runs each in-pod command via ``asyncio.to_thread``,
    which occupies one worker thread for the command's full duration -- for an
    agent run, up to the agent timeout. The default pool is
    ``min(32, cpu_count + 4)`` threads, so on an 18-core host at most 22 trials
    can be executing at once no matter how high ``n_concurrent_trials`` is, while
    the surplus trials still hold their pods. That both wastes cluster time and
    trips NRP's rule against namespaces sitting on idle requests.

    Harbor never replaces the default executor, so raising it here is what lets
    ``n_concurrent_trials`` mean what it says. Pod create/get/delete calls share
    the same pool, so leave headroom above the trial count.

    Returns True once the swap has happened. Callers must tolerate False: agents
    are not always constructed from inside a running loop, and there is no loop to
    configure until there is one.
    """
    global _executor_raised_to
    if _executor_raised_to is not None and _executor_raised_to >= max_workers:
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop yet (synchronous construction path). The caller retries
        # from the first async entry point.
        return False
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="hb-blocking"
        )
    )
    _executor_raised_to = max_workers
    logger.info("Raised asyncio default thread pool to %d workers", max_workers)
    return True


class OpenCodeIdleWatchdog(OpenCode):
    """OpenCode that is killed after a period with no new output.

    Args:
        idle_timeout_sec: Seconds of no growth in the streamed log before the
            OpenCode process group is terminated. Must exceed the longest normal
            gap between events (model latency on a long turn), so keep it well
            above a single request's worst-case duration.
        poll_interval_sec: How often the watchdog samples the log size.
        heartbeat_interval_sec: Seconds between keepalive bytes written to the
            exec stream's stderr; 0 disables. Must stay well under the
            cluster's idle timeout for exec connections (measured at 60s on NRP
            in October 2026). The bytes go to stderr only -- never into the
            streamed log the watchdog measures -- so they keep the connection
            alive without hiding a genuinely hung agent from the watchdog.
    """

    def __init__(
        self,
        *args: Any,
        idle_timeout_sec: int = 600,
        poll_interval_sec: int = 15,
        heartbeat_interval_sec: int = 20,
        max_blocking_threads: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._idle_timeout_sec = int(idle_timeout_sec)
        self._poll_interval_sec = int(poll_interval_sec)
        self._heartbeat_interval_sec = int(heartbeat_interval_sec)
        if self._heartbeat_interval_sec < 0:
            raise ValueError(
                "heartbeat_interval_sec must be >= 0 (0 disables the heartbeat), "
                f"got {self._heartbeat_interval_sec}"
            )
        if self._idle_timeout_sec <= self._poll_interval_sec:
            raise ValueError(
                "idle_timeout_sec must be greater than poll_interval_sec "
                f"(got {self._idle_timeout_sec} <= {self._poll_interval_sec})"
            )
        self._max_blocking_threads = (
            int(max_blocking_threads) if max_blocking_threads is not None else None
        )
        if self._max_blocking_threads is not None:
            # Best effort here; agents are not always built inside a running loop,
            # so exec_as_agent retries before the first in-pod command.
            _raise_blocking_thread_limit(self._max_blocking_threads)

    def _wrap(self, command: str) -> str:
        """Wrap *command* so it is killed after an idle period.

        The inner command reports its own process-group id and exit code through
        files rather than through ``$!``. ``setsid`` forks instead of exec'ing
        whenever its caller is already a process-group leader, and in that case
        ``$!`` names the short-lived ``setsid`` process, not the shell running the
        command: the liveness check then fails immediately, the watchdog loop never
        runs, and the real process keeps the exec stream open until Harbor's agent
        timeout fires. That is what happened on the first cluster run -- every hung
        trial burned the full cap with no watchdog output. Reading the pgid from
        the child itself works under either ``setsid`` branch.

        The exit code of the inner command is preserved unless the watchdog fired,
        in which case 0 is returned: a killed-while-idle agent is not an agent
        failure, and the verifier decides whether the work it left behind counts.

        A background heartbeat writes one byte to stderr every
        ``heartbeat_interval_sec``. The exec runs over a websocket, and NRP's API
        server path drops one that carries no traffic for 60 seconds -- which
        happens on every model call, since the agent is silent while it waits for
        the reply. In August the same connections survived 5-10 minute silences;
        by October every trial died exactly 60s after its last event with
        ``Connection to remote host was lost.`` The heartbeat's ``sleep`` has its
        file descriptors pointed at /dev/null so that, once the loop is killed on
        exit, an orphaned ``sleep`` cannot hold the stream open.
        """
        if self._heartbeat_interval_sec > 0:
            heartbeat = f"""
( while :; do
    sleep {self._heartbeat_interval_sec} </dev/null >/dev/null 2>&1
    printf '.' >&2
  done ) &
hb_pid=$!
trap 'kill "$hb_pid" 2>/dev/null' EXIT
"""
        else:
            heartbeat = ""
        return f"""\
cat > /tmp/hb_opencode_run.sh <<'{_DELIM}'
{command}
{_DELIM}

rm -f /tmp/hb_opencode_pgid /tmp/hb_opencode_rc
{heartbeat}
# The child writes its own pid, which setsid has made both session and process
# group leader, so -$pgid addresses the whole tree including opencode.
setsid sh -c 'echo $$ > /tmp/hb_opencode_pgid; sh /tmp/hb_opencode_run.sh; echo $? > /tmp/hb_opencode_rc' &

pgid=""
waited=0
while [ "$waited" -lt 30 ]; do
    if [ -s /tmp/hb_opencode_pgid ]; then
        pgid=$(cat /tmp/hb_opencode_pgid)
        break
    fi
    sleep 1
    waited=$((waited + 1))
done

idle={self._idle_timeout_sec}
poll={self._poll_interval_sec}
last=-1
stall=0
fired=0

if [ -z "$pgid" ]; then
    # No pgid to watch. Fall back to running unsupervised rather than killing a
    # working agent; the job's agent timeout is still the backstop.
    echo "[harbor-watchdog] could not read child pgid; running without watchdog" >&2
    # Wait for the child's exit-code file, not a bare `wait`: that would also
    # wait on the heartbeat loop, which never ends on its own.
    while [ ! -s /tmp/hb_opencode_rc ]; do
        sleep 1
    done
else
    while kill -s 0 -- -"$pgid" 2>/dev/null; do
        sleep "$poll"
        # Group the redirect so its own failure is silenced too: `2>/dev/null` on
    # wc alone does not cover `<` failing before wc starts.
    cur=$({{ wc -c < {_STREAM_LOG}; }} 2>/dev/null || echo 0)
        if [ "$cur" -eq "$last" ]; then
            stall=$((stall + poll))
        else
            stall=0
            last=$cur
        fi
        if [ "$stall" -ge "$idle" ]; then
            fired=1
            echo "[harbor-watchdog] no new output for ${{idle}}s; terminating opencode" >&2
            kill -s TERM -- -"$pgid" 2>/dev/null || true
            sleep 10
            kill -s KILL -- -"$pgid" 2>/dev/null || true
            break
        fi
    done
fi

rc=0
if [ -s /tmp/hb_opencode_rc ]; then
    rc=$(cat /tmp/hb_opencode_rc)
fi
if [ "$fired" -eq 1 ]; then
    exit 0
fi
exit "$rc"
"""

    @override
    async def exec_as_agent(
        self,
        environment: BaseEnvironment,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        if self._max_blocking_threads is not None:
            _raise_blocking_thread_limit(self._max_blocking_threads)
        if _RUN_MARKER in command:
            command = self._wrap(command)
        return await super().exec_as_agent(
            environment, command, env=env, cwd=cwd, timeout_sec=timeout_sec
        )
