#!/bin/sh
set -eu

mkdir -p /data/runs /data/reports
chown -R capability:capability /data/runs /data/reports

exec gosu capability "$@"
