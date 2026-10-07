#!/bin/sh
# CLAUDE_CODE_SHELL_PREFIX wrapper (D1327). The Claude CLI invokes
#     <this script> '<one complete shell command string>'
# (argc == 1) for EVERYTHING it spawns: Bash-tool commands, hook commands, the
# statusline and MCP stdio servers (permission_server.py included). The
# contract is therefore:
#
#   * Anything that is not a Bash-tool command: `exec` the shell on the string,
#     touching nothing (no tee, no fd changes, no output) -- MCP stdio and hook
#     JSON would break otherwise.
#   * A Bash-tool command (recognised by its shape, see below): run it through
#     the same shell, unchanged, mirroring its stdout and stderr into a
#     per-command log under $FUSED_CLAUDE_CMD_LOG (a directory, one per chat)
#     so the drawer's read-only "Claude" tab can show it live. The caller sees
#     the same bytes on the same fds and the same exit status.
#
# Per command, in $FUSED_CLAUDE_CMD_LOG:
#   <id>.meta   pid=, pgid=, start= (epoch seconds); written FIRST
#   <id>.cmd    the full command string, raw; created atomically (tmp + mv)
#               after .meta, so a listed .cmd always has its .meta
#   <id>.out    stdout+stderr as they arrived
#   <id>.exit   the exit status, written last
# <id> is "<epoch seconds, 11 digits>-<pid>", so a name sort is start order.
# Any failure to set the logging up falls back to the transparent exec.

cmd=$1

# The CLI's command strings are written for bash/zsh. $SHELL can be anything
# (fish breaks MCP servers, hooks and the permission server), so it is only
# honoured when it is one of those.
# Transparent path: $CLAUDE_CODE_SHELL, else $SHELL if bash/zsh/sh, else the
# first executable of /bin/bash, /bin/zsh, /bin/sh.
sh_bin=
if [ -n "$CLAUDE_CODE_SHELL" ]; then
  sh_bin=$CLAUDE_CODE_SHELL
else
  case ${SHELL##*/} in
    bash|zsh|sh) [ -x "$SHELL" ] && sh_bin=$SHELL ;;
  esac
fi
if [ -z "$sh_bin" ] || [ ! -x "$sh_bin" ]; then
  sh_bin=/bin/sh
  for s in /bin/bash /bin/zsh /bin/sh; do
    [ -x "$s" ] && { sh_bin=$s; break; }
  done
fi

transparent() {
  exec "$sh_bin" -c "$cmd"
}

# Bash-tool shape: sources the shell snapshot and ends by persisting the cwd to
# a `...-cwd` file. Conservative on purpose; hooks and MCP servers never match.
case $cmd in
  *'/shell-snapshots/snapshot-'*'pwd -P >| '*'-cwd'*) ;;
  *) transparent ;;
esac

# Bash-tool path: the snapshot is named snapshot-<shell>-..., and that shell
# (zsh or bash) must run it: $CLAUDE_CODE_SHELL if executable, else $SHELL if
# its basename matches, else `command -v`, else /bin/<shell>.
snap=${cmd#*'/shell-snapshots/snapshot-'}
snap=${snap%%-*}
case $snap in
  zsh|bash)
    want=
    if [ -n "$CLAUDE_CODE_SHELL" ] && [ -x "$CLAUDE_CODE_SHELL" ]; then
      want=$CLAUDE_CODE_SHELL
    elif [ "${SHELL##*/}" = "$snap" ] && [ -x "$SHELL" ]; then
      want=$SHELL
    else
      want=$(command -v "$snap" 2>/dev/null)
      [ -x "$want" ] || want=/bin/$snap
    fi
    [ -x "$want" ] && sh_bin=$want
    ;;
esac

dir=$FUSED_CLAUDE_CMD_LOG
[ -n "$dir" ] || transparent
mkdir -p "$dir" 2>/dev/null || transparent
[ -d "$dir" ] && [ -w "$dir" ] || transparent

id=$(printf '%011d-%s' "$(date +%s)" "$$")
base=$dir/$id
fo=$base.fo
fe=$base.fe
mkfifo "$fo" "$fe" 2>/dev/null || { rm -f "$fo" "$fe"; transparent; }

pgid=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
{ printf 'pid=%s\npgid=%s\nstart=%s\n' "$$" "$pgid" "$(date +%s)" > "$base.meta" \
  && printf '%s' "$cmd" > "$base.cmd.tmp" && mv "$base.cmd.tmp" "$base.cmd"; } 2>/dev/null \
  || { rm -f "$fo" "$fe" "$base.meta" "$base.cmd.tmp" "$base.cmd"; transparent; }
: > "$base.out"

# Keep the caller's stdin for the command (a background job would otherwise get
# /dev/null), and the caller's stdout/stderr for the tees.
exec 3<&0
tee -a "$base.out" < "$fo" &
t1=$!
tee -a "$base.out" < "$fe" >&2 &
t2=$!

# A non-interactive sh starts an async job with SIGINT/SIGQUIT ignored (POSIX),
# and a child cannot undo an inherited SIG_IGN via `trap` (POSIX: a signal
# ignored on entry to a non-interactive shell cannot be trapped or reset), so
# KeyboardInterrupt, `kill -INT` and test-runner cancellation would all be
# dead inside the command. `set -m` gives the job its own process group and
# default dispositions, but only by way of job control, which needs a real
# controlling tty -- dash (and other shells) silently drop it otherwise and
# leave both problems in place, while also warning on stderr. A plain
# sigaction() has no such restriction, so python3 resets SIGINT/SIGQUIT and
# claims a fresh process group before taking over the launch; without
# python3 the job runs as before (inherited ignore, shared process group).
if launcher=$(command -v python3 2>/dev/null); then
  "$launcher" -c '
import os, signal, sys
signal.signal(signal.SIGINT, signal.SIG_DFL)
signal.signal(signal.SIGQUIT, signal.SIG_DFL)
try:
    os.setpgid(0, 0)
except OSError:
    pass
os.execvp(sys.argv[1], sys.argv[1:])
' "$sh_bin" -c "$cmd" <&3 >"$fo" 2>"$fe" &
else
  "$sh_bin" -c "$cmd" <&3 >"$fo" 2>"$fe" &
fi
cpid=$!
# A signal aimed at this wrapper must reach the command (now its own process
# group, led by $cpid), which is no longer the wrapper itself.
trap 'kill -TERM -- -"$cpid" 2>/dev/null || kill -TERM "$cpid" 2>/dev/null' TERM INT HUP
wait "$cpid"
rc=$?
while kill -0 "$cpid" 2>/dev/null; do
  wait "$cpid"
  rc=$?
done
trap - TERM INT HUP
exec 3<&-

# Let the tees drain (they end when the last writer closes the fifo). A daemon
# the command left holding the fifo must not hang us: give up after ~2s.
n=0
while [ "$n" -lt 40 ] && { kill -0 "$t1" 2>/dev/null || kill -0 "$t2" 2>/dev/null; }; do
  sleep 0.05
  n=$((n + 1))
done
rm -f "$fo" "$fe"
printf '%s\n' "$rc" > "$base.exit"
exit "$rc"
