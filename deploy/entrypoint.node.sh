#!/bin/sh
set -e

cd /work

if [ ! -f "${HOME}/.config/termflow/config.json" ] && [ -n "${TERMFLOW_SERVER}" ] && [ -n "${TERMFLOW_CODE}" ]; then
    login_command="termflow login --server ${TERMFLOW_SERVER} --code ${TERMFLOW_CODE}"
    if [ "${TERMFLOW_ALLOW_INSECURE_HTTP}" = "true" ]; then
        login_command="${login_command} --allow-insecure-http"
    fi
    ${login_command}
fi

if [ -n "${TERMFLOW_NEW}" ]; then
    exec termflow new --name "${TERMFLOW_NEW}"
fi

exec "$@"
