#!/bin/sh
set -eu

data_dir=/app/data
totp_dir=/app/totp-secrets

init_mount_points() {
    for dir in "${data_dir}" "${totp_dir}"; do
        if [ -L "${dir}" ]; then
            echo "refusing symlinked mount point: ${dir}" >&2
            exit 1
        fi
        if [ ! -d "${dir}" ]; then
            echo "mount point is not a directory: ${dir}" >&2
            exit 1
        fi
        chown termflow:termflow "${dir}"
        find "${dir}" -xdev -exec chown termflow:termflow {} +
    done
}

if [ "$(id -u)" = "0" ]; then
    init_mount_points
    exec setpriv --reuid termflow --regid termflow --clear-groups -- "$@"
fi

exec "$@"
