#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cert_dir="${script_dir}/../certs"
mkdir -p "${cert_dir}"

openssl req \
  -x509 \
  -nodes \
  -newkey rsa:2048 \
  -keyout "${cert_dir}/key.pem" \
  -out "${cert_dir}/cert.pem" \
  -days 365 \
  -subj "/CN=127.0.0.1"

echo "Created ${cert_dir}/cert.pem and ${cert_dir}/key.pem"
