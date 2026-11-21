#!/usr/bin/env bash
# 봇 실행: bash run.sh   (또는 ./run.sh)
# 가상환경(.venv / venv)이 있으면 그걸 쓰고, 없으면 시스템 python3 를 쓴다. 종료는 Ctrl+C.
cd "$(dirname "$(readlink -f "$0")")" || exit 1

if [ -x .venv/bin/python ]; then PY=.venv/bin/python
elif [ -x venv/bin/python ]; then PY=venv/bin/python
else PY=python3; fi

exec "$PY" -m bot.main "$@"
