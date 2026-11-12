#!/bin/sh
# Usage: with-secret.sh VAR=/path/to/file [VAR2=/path ...] -- command [args...]
#
# Reads each file, exports its trimmed content as VAR and execs the command,
# so a third-party MCP server gets its token through the environment without
# the token ever appearing in Claude Code settings or on a command line.
set -eu

while [ "$#" -gt 0 ]; do
  case "$1" in
    --) shift; break ;;
    *=*)
      var=${1%%=*}
      file=${1#*=}
      case "$file" in \~/*) file="$HOME/${file#\~/}" ;; esac
      case "$var" in
        *[!A-Za-z0-9_]* | "" | [0-9]*) echo "with-secret: bad variable name '$var'" >&2; exit 64 ;;
      esac
      if [ ! -r "$file" ]; then
        echo "with-secret: $var: cannot read $file (see docs/SETUP.md)" >&2
        exit 66
      fi
      value=$(tr -d '\r\n' < "$file")
      if [ -z "$value" ]; then
        echo "with-secret: $var: $file is empty" >&2
        exit 66
      fi
      export "$var=$value"
      shift
      ;;
    *) echo "with-secret: expected VAR=file or --, got '$1'" >&2; exit 64 ;;
  esac
done

[ "$#" -gt 0 ] || { echo "with-secret: no command after --" >&2; exit 64; }
exec "$@"
