#!/bin/sh
set -eu

home_dir=/home/termflow
work_dir=/work

init_mount_points() {
    for dir in "${home_dir}" "${work_dir}"; do
        if [ -L "${dir}" ]; then
            echo "refusing symlinked mount point: ${dir}" >&2
            exit 1
        fi
        if [ ! -d "${dir}" ]; then
            echo "mount point is not a directory: ${dir}" >&2
            exit 1
        fi
        chown -h termflow:termflow "${dir}"
        find "${dir}" -xdev -exec chown -h termflow:termflow {} +
    done
}

if [ "$(id -u)" = "0" ]; then
    init_mount_points
    exec setpriv --reuid termflow --regid termflow --clear-groups -- "$0" "$@"
fi

HOME=${home_dir}
export HOME
cd "${work_dir}"

case "${TERMFLOW_SHELL:-bash}" in
    bash)
        SHELL=/bin/bash
        ;;
    sh)
        SHELL=/bin/sh
        ;;
    *)
        echo "invalid TERMFLOW_SHELL: expected bash or sh" >&2
        exit 64
        ;;
esac
export SHELL

if [ ! -f "${HOME}/.config/termflow/config.json" ] && [ -n "${TERMFLOW_SERVER:-}" ] && [ -n "${TERMFLOW_CODE:-}" ]; then
    login_command="termflow login --server ${TERMFLOW_SERVER} --code ${TERMFLOW_CODE}"
    if [ "${TERMFLOW_ALLOW_INSECURE_HTTP:-}" = "true" ]; then
        login_command="${login_command} --allow-insecure-http"
    fi
    ${login_command}
fi

if [ -n "${TERMFLOW_NEW:-}" ]; then
    exec termflow serve --name "${TERMFLOW_NEW}"
fi

exec "$@"
