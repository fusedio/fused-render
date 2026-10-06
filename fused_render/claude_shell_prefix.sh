#!/bin/sh
# CLAUDE_CODE_SHELL_PREFIX wrapper (D1326). The Claude CLI invokes
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
#   <id>.cmd    the full command string, raw
#   <id>.meta   pid=, pgid=, start= (epoch seconds)
#   <id>.out    stdout+stderr as they arrived
#   <id>.exit   the exit status, written last
# <id> is "<epoch seconds, 11 digits>-<pid>", so a name sort is start order.
# Any failure to set the logging up falls back to the transparent exec.

cmd=$1
sh_bin=${CLAUDE_CODE_SHELL:-${SHELL:-/bin/sh}}
[ -x "$sh_bin" ] || sh_bin=/bin/sh

transparent() {
  exec "$sh_bin" -c "$cmd"
}

# Bash-tool shape: sources the shell snapshot and ends by persisting the cwd to
# a `...-cwd` file. Conservative on purpose; hooks and MCP servers never match.
case $cmd in
  *'/shell-snapshots/snapshot-'*'pwd -P >| '*'-cwd'*) ;;
  *) transparent ;;
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

printf '%s' "$cmd" > "$base.cmd" 2>/dev/null || { rm -f "$fo" "$fe"; transparent; }
pgid=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
printf 'pid=%s\npgid=%s\nstart=%s\n' "$$" "$pgid" "$(date +%s)" > "$base.meta"
: > "$base.out"

# Keep the caller's stdin for the command (a background job would otherwise get
# /dev/null), and the caller's stdout/stderr for the tees.
exec 3<&0
tee -a "$base.out" < "$fo" &
t1=$!
tee -a "$base.out" < "$fe" >&2 &
t2=$!

"$sh_bin" -c "$cmd" <&3 >"$fo" 2>"$fe" &
cpid=$!
# A signal aimed at this wrapper must reach the command, which is no longer
# the wrapper itself.
trap 'kill -TERM "$cpid" 2>/dev/null' TERM INT HUP
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
